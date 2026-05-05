import streamlit as st
import pandas as pd
import anthropic
import json
import os
import sys
import subprocess
import tempfile
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Chat ERP Teleport",
    page_icon="📊",
    layout="wide"
)

SYSTEM_PROMPT = """Você é um assistente especializado em análise de dados para corretoras de seguros. \
Você tem acesso a dados exportados do sistema ERP Teleport carregados pelo usuário.

Suas especialidades:
- Análise de apólices e sinistros
- Consultas sobre clientes e contratos
- Análise financeira: comissões, prêmios, pagamentos
- Relatórios gerenciais e resumos executivos

Instruções:
1. Use SEMPRE as ferramentas disponíveis para buscar dados antes de responder
2. Baseie suas respostas exclusivamente nos dados reais retornados pelas ferramentas
3. Formate valores monetários no padrão brasileiro (R$ 1.234,56)
4. Datas no formato brasileiro (DD/MM/AAAA)
5. Responda em português brasileiro
6. Quando não encontrar dados relevantes, informe claramente"""

TOOLS = [
    {
        "name": "listar_tabelas",
        "description": "Lista todas as tabelas carregadas com total de registros e colunas disponíveis",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "obter_info_tabela",
        "description": "Obtém informações detalhadas de uma tabela: colunas, tipos e amostra de registros",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_tabela": {"type": "string", "description": "Nome exato da tabela"}
            },
            "required": ["nome_tabela"]
        }
    },
    {
        "name": "consultar_dados",
        "description": "Filtra e retorna registros de uma tabela. Use para buscas específicas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_tabela": {"type": "string"},
                "filtro": {
                    "type": "string",
                    "description": "Filtro pandas query. Ex: 'Nome.str.contains(\"Cristiano\", case=False)' ou 'Status == \"Ativo\"'"
                },
                "colunas": {"type": "array", "items": {"type": "string"}},
                "limite": {"type": "integer", "description": "Máximo de linhas (padrão 50)"},
                "ordenar_por": {"type": "string"},
                "ordem_decrescente": {"type": "boolean"}
            },
            "required": ["nome_tabela"]
        }
    },
    {
        "name": "agregar_dados",
        "description": "Agrupa e sumariza dados: totais, médias, contagens por categoria",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_tabela": {"type": "string"},
                "agrupar_por": {"type": "array", "items": {"type": "string"}},
                "operacao": {
                    "type": "string",
                    "enum": ["sum", "count", "mean", "min", "max", "nunique"]
                },
                "coluna_valor": {"type": "string"},
                "filtro": {"type": "string"}
            },
            "required": ["nome_tabela", "agrupar_por", "operacao"]
        }
    },
    {
        "name": "calcular_estatisticas",
        "description": "Calcula estatísticas descritivas de colunas numéricas",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_tabela": {"type": "string"},
                "colunas": {"type": "array", "items": {"type": "string"}},
                "filtro": {"type": "string"}
            },
            "required": ["nome_tabela", "colunas"]
        }
    }
]


def execute_tool(tool_name: str, tool_input: dict) -> str:
    dfs: dict[str, pd.DataFrame] = st.session_state.get("dataframes", {})

    try:
        if tool_name == "listar_tabelas":
            if not dfs:
                return "Nenhuma tabela carregada ainda."
            linhas = [f"- **{n}**: {len(df):,} registros, {len(df.columns)} colunas" for n, df in dfs.items()]
            return "Tabelas disponíveis:\n" + "\n".join(linhas)

        elif tool_name == "obter_info_tabela":
            nome = tool_input["nome_tabela"]
            if nome not in dfs:
                return f"Tabela '{nome}' não encontrada. Use listar_tabelas."
            df = dfs[nome]
            info = {
                "total_registros": len(df),
                "colunas": {col: {"tipo": str(df[col].dtype), "nulos": int(df[col].isnull().sum())} for col in df.columns}
            }
            sample = df.head(3).to_dict(orient="records")
            return (
                f"**'{nome}'** — {len(df):,} registros\n"
                f"```json\n{json.dumps(info, ensure_ascii=False, indent=2)}\n```\n"
                f"**Amostra:**\n```json\n{json.dumps(sample, ensure_ascii=False, indent=2, default=str)}\n```"
            )

        elif tool_name == "consultar_dados":
            nome = tool_input["nome_tabela"]
            if nome not in dfs:
                return f"Tabela '{nome}' não encontrada."
            df = dfs[nome].copy()

            filtro = tool_input.get("filtro")
            if filtro:
                try:
                    df = df.query(filtro, engine="python")
                except Exception as e:
                    return f"Erro no filtro '{filtro}': {e}\nColunas disponíveis: {list(dfs[nome].columns)}"

            colunas = tool_input.get("colunas") or []
            if colunas:
                validas = [c for c in colunas if c in df.columns]
                if validas:
                    df = df[validas]

            ordenar_por = tool_input.get("ordenar_por")
            if ordenar_por and ordenar_por in df.columns:
                df = df.sort_values(by=ordenar_por, ascending=not tool_input.get("ordem_decrescente", False))

            limite = min(tool_input.get("limite", 50), 200)
            total = len(df)
            records = df.head(limite).to_dict(orient="records")
            return (
                f"**{total:,} registros encontrados** (exibindo {len(records)}):\n"
                f"```json\n{json.dumps(records, ensure_ascii=False, indent=2, default=str)}\n```"
            )

        elif tool_name == "agregar_dados":
            nome = tool_input["nome_tabela"]
            if nome not in dfs:
                return f"Tabela '{nome}' não encontrada."
            df = dfs[nome].copy()

            filtro = tool_input.get("filtro")
            if filtro:
                try:
                    df = df.query(filtro, engine="python")
                except Exception as e:
                    return f"Erro no filtro: {e}"

            agrupar_por = tool_input["agrupar_por"]
            operacao = tool_input["operacao"]
            coluna_valor = tool_input.get("coluna_valor")

            for col in agrupar_por:
                if col not in df.columns:
                    return f"Coluna '{col}' não encontrada. Disponíveis: {list(df.columns)}"

            grouped = df.groupby(agrupar_por)
            op_map = {"sum": "sum", "mean": "mean", "min": "min", "max": "max", "nunique": "nunique"}

            if operacao == "count":
                result_df = grouped.size().reset_index(name="contagem")
            elif coluna_valor:
                if coluna_valor not in df.columns:
                    return f"Coluna '{coluna_valor}' não encontrada."
                result_df = grouped[coluna_valor].agg(op_map[operacao]).reset_index()
            else:
                numericas = [c for c in df.select_dtypes(include="number").columns if c not in agrupar_por]
                if not numericas:
                    return "Nenhuma coluna numérica encontrada. Especifique 'coluna_valor'."
                result_df = grouped[numericas].agg(op_map[operacao]).reset_index()

            result_df = result_df.sort_values(result_df.columns[-1], ascending=False)
            records = result_df.to_dict(orient="records")
            return f"**Resultado ({operacao}):**\n```json\n{json.dumps(records, ensure_ascii=False, indent=2, default=str)}\n```"

        elif tool_name == "calcular_estatisticas":
            nome = tool_input["nome_tabela"]
            if nome not in dfs:
                return f"Tabela '{nome}' não encontrada."
            df = dfs[nome].copy()

            filtro = tool_input.get("filtro")
            if filtro:
                try:
                    df = df.query(filtro, engine="python")
                except Exception as e:
                    return f"Erro no filtro: {e}"

            colunas = [c for c in tool_input["colunas"] if c in df.columns]
            if not colunas:
                return f"Nenhuma coluna válida. Disponíveis: {list(df.columns)}"

            stats = {}
            for col in colunas:
                try:
                    stats[col] = {
                        "total": float(df[col].sum()),
                        "média": float(df[col].mean()),
                        "mínimo": float(df[col].min()),
                        "máximo": float(df[col].max()),
                        "mediana": float(df[col].median()),
                        "nulos": int(df[col].isnull().sum()),
                    }
                except Exception:
                    stats[col] = {"erro": "Coluna não numérica"}

            return f"**Estatísticas:**\n```json\n{json.dumps(stats, ensure_ascii=False, indent=2)}\n```"

        return f"Ferramenta '{tool_name}' não reconhecida."

    except Exception as e:
        return f"Erro ao executar '{tool_name}': {e}"


def content_to_dict(content) -> list[dict]:
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    result = []
    for block in content:
        if isinstance(block, dict):
            result.append(block)
        elif hasattr(block, "type"):
            if block.type == "text":
                result.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                result.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return result


def get_display_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts)
    return str(content)


def process_message(user_message: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("ANTHROPIC_API_KEY não configurada no arquivo .env")
        st.stop()

    client = anthropic.Anthropic(api_key=api_key)
    st.session_state.api_messages.append({"role": "user", "content": user_message})

    final_text = ""

    while True:
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=st.session_state.api_messages,
            output_config={"effort": "medium"},
        )

        assistant_content = content_to_dict(response.content)
        st.session_state.api_messages.append({"role": "assistant", "content": assistant_content})

        for block in assistant_content:
            if block.get("type") == "text":
                final_text += block["text"]

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in assistant_content:
                if block.get("type") == "tool_use":
                    result = execute_tool(block["name"], block["input"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result,
                    })
            st.session_state.api_messages.append({"role": "user", "content": tool_results})
        else:
            break

    return final_text


def load_file(uploaded_file) -> pd.DataFrame | None:
    name = uploaded_file.name.lower()
    try:
        if name.endswith(".csv"):
            for enc in ("utf-8", "latin-1", "cp1252"):
                try:
                    uploaded_file.seek(0)
                    return pd.read_csv(uploaded_file, encoding=enc, sep=None, engine="python")
                except UnicodeDecodeError:
                    continue
        elif name.endswith((".xlsx", ".xls")):
            return pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"Erro ao ler {uploaded_file.name}: {e}")
    return None


def main():
    if "api_messages" not in st.session_state:
        st.session_state.api_messages = []
    if "dataframes" not in st.session_state:
        st.session_state.dataframes = {}

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.title("📂 Dados do Teleport")

        uploaded_files = st.file_uploader(
            "Arraste os arquivos XLS/CSV exportados do Teleport",
            type=["csv", "xlsx", "xls"],
            accept_multiple_files=True,
        )

        if uploaded_files:
            for f in uploaded_files:
                key = os.path.splitext(f.name)[0]
                if key not in st.session_state.dataframes:
                    df = load_file(f)
                    if df is not None:
                        st.session_state.dataframes[key] = df
                        st.success(f"✅ **{f.name}** — {len(df):,} registros")

        if st.session_state.dataframes:
            st.divider()
            st.subheader("Tabelas carregadas")
            for name, df in st.session_state.dataframes.items():
                with st.expander(f"📋 {name} ({len(df):,} registros)"):
                    st.dataframe(df.head(5), use_container_width=True)

            st.divider()
            col1, col2 = st.columns(2)
            if col1.button("🗑️ Limpar dados", use_container_width=True):
                st.session_state.dataframes = {}
                st.session_state.api_messages = []
                st.rerun()
            if col2.button("🔄 Nova conversa", use_container_width=True):
                st.session_state.api_messages = []
                st.rerun()

    # ── Main chat ─────────────────────────────────────────────────────────────
    st.title("💬 Chat com dados ERP Teleport")

    if not st.session_state.dataframes:
        st.info("👈 Faça upload dos arquivos exportados do Teleport para começar.")
        st.markdown("""
### Como usar:
1. No Teleport, exporte os relatórios desejados (XLS ou CSV)
2. Arraste os arquivos para a barra lateral
3. Faça perguntas em português sobre seus dados

### Exemplos de perguntas:
- "Quantos clientes com nome Cristiano eu tenho?"
- "Qual o total de prêmios por seguradora?"
- "Liste as apólices que vencem esse mês"
- "Qual a comissão total do último trimestre?"
- "Mostre os 10 maiores clientes por volume de apólices"
        """)
        return

    # Exibe conversa
    for msg in st.session_state.api_messages:
        role = msg["role"]
        content = msg["content"]
        if role == "user" and isinstance(content, list):
            if all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
        text = get_display_text(content)
        if not text.strip():
            continue
        with st.chat_message(role):
            st.markdown(text)

    if prompt := st.chat_input("Faça uma pergunta sobre seus dados…"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Analisando dados…"):
                response_text = process_message(prompt)
            st.markdown(response_text)


if __name__ == "__main__":
    main()
