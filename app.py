import streamlit as st
import pandas as pd
import anthropic
import json
import os
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Chat ERP Teleport",
    page_icon="📊",
    layout="wide"
)

SYSTEM_PROMPT = """Você é um assistente especializado em análise de dados para corretoras de seguros. \
Você tem acesso a dados exportados do sistema ERP Teleport.

Suas especialidades:
- Análise de apólices e sinistros
- Consultas sobre clientes e contratos
- Análise financeira: comissões, prêmios, pagamentos
- Relatórios gerenciais e resumos executivos

Instruções:
1. Use SEMPRE as ferramentas disponíveis para buscar dados antes de responder
2. Seja preciso e baseie suas respostas nos dados reais das tabelas
3. Formate valores monetários no padrão brasileiro (R$ 1.234,56)
4. Responda em português brasileiro
5. Quando não encontrar dados relevantes, informe claramente"""

TOOLS = [
    {
        "name": "listar_tabelas",
        "description": "Lista todas as tabelas (arquivos CSV/Excel) carregadas com total de registros e colunas",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "obter_info_tabela",
        "description": "Obtém informações detalhadas sobre uma tabela: colunas, tipos de dados, total de registros e amostra dos primeiros registros",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_tabela": {
                    "type": "string",
                    "description": "Nome exato da tabela (conforme retornado por listar_tabelas)"
                }
            },
            "required": ["nome_tabela"]
        }
    },
    {
        "name": "consultar_dados",
        "description": "Filtra e retorna registros de uma tabela. Use para buscar registros específicos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_tabela": {
                    "type": "string",
                    "description": "Nome da tabela para consultar"
                },
                "filtro": {
                    "type": "string",
                    "description": "Filtro em formato pandas query. Exemplos: 'status == \"ativo\"', 'valor > 1000', 'nome.str.contains(\"Silva\", case=False)', 'data >= \"2024-01-01\"'"
                },
                "colunas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Colunas específicas a retornar. Se vazio, retorna todas."
                },
                "limite": {
                    "type": "integer",
                    "description": "Número máximo de linhas (padrão: 50, máximo: 200)"
                },
                "ordenar_por": {
                    "type": "string",
                    "description": "Coluna para ordenar os resultados"
                },
                "ordem_decrescente": {
                    "type": "boolean",
                    "description": "Se verdadeiro, ordena de forma decrescente"
                }
            },
            "required": ["nome_tabela"]
        }
    },
    {
        "name": "agregar_dados",
        "description": "Agrupa e sumariza dados: totais, médias, contagens, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_tabela": {"type": "string", "description": "Nome da tabela"},
                "agrupar_por": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Colunas para agrupar (ex: ['status', 'tipo_seguro'])"
                },
                "operacao": {
                    "type": "string",
                    "enum": ["sum", "count", "mean", "min", "max", "nunique"],
                    "description": "Operação: sum=soma, count=contagem, mean=média, min=mínimo, max=máximo, nunique=qtd únicos"
                },
                "coluna_valor": {
                    "type": "string",
                    "description": "Coluna numérica para aplicar a operação (não necessário para 'count')"
                },
                "filtro": {
                    "type": "string",
                    "description": "Filtro opcional antes de agregar (formato pandas query)"
                }
            },
            "required": ["nome_tabela", "agrupar_por", "operacao"]
        }
    },
    {
        "name": "calcular_estatisticas",
        "description": "Calcula estatísticas descritivas de colunas numéricas (total, média, mínimo, máximo, mediana)",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_tabela": {"type": "string", "description": "Nome da tabela"},
                "colunas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Colunas numéricas para calcular estatísticas"
                },
                "filtro": {
                    "type": "string",
                    "description": "Filtro opcional (formato pandas query)"
                }
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
                return "Nenhuma tabela carregada. Faça upload de arquivos CSV ou Excel na barra lateral."
            linhas = [f"- **{n}**: {len(df)} registros, {len(df.columns)} colunas" for n, df in dfs.items()]
            return "Tabelas disponíveis:\n" + "\n".join(linhas)

        elif tool_name == "obter_info_tabela":
            nome = tool_input["nome_tabela"]
            if nome not in dfs:
                return f"Tabela '{nome}' não encontrada. Use listar_tabelas para ver as tabelas disponíveis."
            df = dfs[nome]
            info = {
                "total_registros": len(df),
                "total_colunas": len(df.columns),
                "colunas": {
                    col: {
                        "tipo": str(df[col].dtype),
                        "nulos": int(df[col].isnull().sum()),
                        "valores_unicos": int(df[col].nunique()),
                    }
                    for col in df.columns
                },
            }
            sample = df.head(5).to_dict(orient="records")
            return (
                f"**Tabela '{nome}':**\n"
                f"```json\n{json.dumps(info, ensure_ascii=False, indent=2)}\n```\n\n"
                f"**Primeiros registros:**\n"
                f"```json\n{json.dumps(sample, ensure_ascii=False, indent=2, default=str)}\n```"
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
                    return f"Erro no filtro '{filtro}': {e}"

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
                f"**{total} registros encontrados** (exibindo {len(records)}):\n"
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
                numericas = df.select_dtypes(include="number").columns.tolist()
                numericas = [c for c in numericas if c not in agrupar_por]
                if not numericas:
                    return "Nenhuma coluna numérica encontrada. Especifique 'coluna_valor'."
                result_df = grouped[numericas].agg(op_map[operacao]).reset_index()

            result_df = result_df.sort_values(result_df.columns[-1], ascending=False)
            records = result_df.to_dict(orient="records")
            return (
                f"**Resultado ({operacao}):**\n"
                f"```json\n{json.dumps(records, ensure_ascii=False, indent=2, default=str)}\n```"
            )

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
                return f"Nenhuma coluna válida encontrada. Disponíveis: {list(df.columns)}"

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
                    stats[col] = {"erro": "Não é possível calcular estatísticas para esta coluna"}

            return f"**Estatísticas:**\n```json\n{json.dumps(stats, ensure_ascii=False, indent=2)}\n```"

        return f"Ferramenta '{tool_name}' não reconhecida."

    except Exception as e:
        return f"Erro ao executar '{tool_name}': {e}"


def content_to_dict(content) -> list[dict]:
    """Converts Anthropic content blocks to plain dicts."""
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
    """Extracts only text blocks from a message's content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block["text"])
            elif hasattr(block, "type") and block.type == "text":
                parts.append(block.text)
        return "\n".join(parts)
    return str(content)


def process_message(user_message: str) -> str:
    """Sends user message to Claude, runs tool loop, returns final text response."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("Variável ANTHROPIC_API_KEY não encontrada. Configure o arquivo .env")
        st.stop()

    client = anthropic.Anthropic(api_key=api_key)

    # Add user message to history
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

        # Collect any text in this response
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
    name = uploaded_file.name
    if name.lower().endswith(".csv"):
        for encoding in ("utf-8", "latin-1", "cp1252"):
            try:
                uploaded_file.seek(0)
                return pd.read_csv(uploaded_file, encoding=encoding, sep=None, engine="python")
            except UnicodeDecodeError:
                continue
        return None
    elif name.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(uploaded_file)
    return None


def main():
    if "api_messages" not in st.session_state:
        st.session_state.api_messages = []
    if "dataframes" not in st.session_state:
        st.session_state.dataframes = {}

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.title("📂 Dados do Teleport")
        st.caption("Exporte os relatórios do Teleport ERP e faça upload aqui")

        uploaded_files = st.file_uploader(
            "Selecione os arquivos",
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
                    else:
                        st.error(f"❌ Erro ao carregar {f.name}")

        if st.session_state.dataframes:
            st.divider()
            st.subheader("Tabelas carregadas")
            for name, df in st.session_state.dataframes.items():
                with st.expander(f"📋 {name}"):
                    st.caption(f"{len(df):,} registros · {len(df.columns)} colunas")
                    st.dataframe(df.head(3), use_container_width=True)

            st.divider()
            if st.button("🗑️ Remover todos os dados", use_container_width=True):
                st.session_state.dataframes = {}
                st.session_state.api_messages = []
                st.rerun()

        st.divider()
        st.caption(
            "💡 **Como exportar do Teleport:** acesse o relatório desejado, "
            "clique em exportar e escolha CSV ou Excel."
        )

    # ── Main content ─────────────────────────────────────────────────────────
    st.title("💬 Chat com dados ERP Teleport")

    if not st.session_state.dataframes:
        st.info("👈 Faça upload dos arquivos exportados do Teleport para começar.")
        st.markdown("""
### Como usar:
1. **Exporte os dados** do Teleport ERP (CSV ou Excel)
2. **Faça upload** na barra lateral à esquerda
3. **Pergunte** em português sobre seus dados

### Exemplos de perguntas:
- "Quantas apólices ativas eu tenho?"
- "Quais clientes têm sinistros em aberto?"
- "Qual o total de prêmios recebidos em 2024?"
- "Liste os 10 clientes com maior volume de apólices"
- "Qual a comissão total por ramo de seguro?"
- "Mostre um resumo financeiro do mês passado"
        """)
        return

    # Display conversation (only user messages and assistant text)
    display_messages = [
        m for m in st.session_state.api_messages
        if m["role"] in ("user", "assistant") and isinstance(m["content"], (str, list))
    ]

    for msg in display_messages:
        role = msg["role"]
        content = msg["content"]

        # Skip messages that contain only tool results (user role, list of tool_result dicts)
        if role == "user" and isinstance(content, list):
            if all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue

        text = get_display_text(content)
        if not text.strip():
            continue

        with st.chat_message(role):
            st.markdown(text)

    # Chat input
    if prompt := st.chat_input("Faça uma pergunta sobre seus dados..."):
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Consultando dados..."):
                response_text = process_message(prompt)
            st.markdown(response_text)

    # Clear chat
    if st.session_state.api_messages:
        if st.button("🔄 Nova conversa", help="Limpa o histórico do chat"):
            st.session_state.api_messages = []
            st.rerun()


if __name__ == "__main__":
    main()
