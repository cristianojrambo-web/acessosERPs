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
Você tem acesso direto ao sistema ERP Teleport via ferramentas integradas — não precisa que o usuário exporte nada.

Suas especialidades:
- Análise de apólices e sinistros
- Consultas sobre clientes e contratos
- Análise financeira: comissões, prêmios, pagamentos
- Relatórios gerenciais e resumos executivos

Instruções:
1. Use SEMPRE as ferramentas para buscar dados do Teleport antes de responder
2. Baseie suas respostas exclusivamente nos dados reais retornados pelas ferramentas
3. Formate valores monetários no padrão brasileiro (R$ 1.234,56)
4. Responda em português brasileiro
5. Se uma ferramenta retornar erro de login ou conexão, informe o usuário claramente"""

TOOLS = [
    {
        "name": "buscar_clientes",
        "description": "Busca clientes e segurados no Teleport ERP. Use para encontrar informações sobre clientes específicos ou listar todos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "busca": {
                    "type": "string",
                    "description": "Termo de busca: nome, CPF, CNPJ ou email. Deixe vazio para listar todos."
                }
            },
            "required": []
        }
    },
    {
        "name": "buscar_apolices",
        "description": "Busca apólices de seguro no Teleport ERP. Use para consultar apólices ativas, vencidas, por cliente ou seguradora.",
        "input_schema": {
            "type": "object",
            "properties": {
                "busca": {
                    "type": "string",
                    "description": "Número da apólice, nome do segurado, seguradora ou status. Vazio para listar todas."
                }
            },
            "required": []
        }
    },
    {
        "name": "buscar_sinistros",
        "description": "Busca sinistros no Teleport ERP.",
        "input_schema": {
            "type": "object",
            "properties": {
                "busca": {
                    "type": "string",
                    "description": "Número do sinistro, nome do segurado ou status. Vazio para listar todos."
                }
            },
            "required": []
        }
    },
    {
        "name": "consultar_financeiro",
        "description": "Consulta dados financeiros no Teleport ERP: comissões a receber, repasses, extratos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "busca": {
                    "type": "string",
                    "description": "Período, tipo de comissão ou termo de busca. Vazio para listar tudo."
                }
            },
            "required": []
        }
    }
]

SECTION_MAP = {
    "buscar_clientes": "clientes",
    "buscar_apolices": "apolices",
    "buscar_sinistros": "sinistros",
    "consultar_financeiro": "financeiro",
}


def _query_teleport(section: str) -> str:
    """Executa o scraper como subprocesso e retorna os dados da seção."""
    scraper_path = Path(__file__).parent / "scraper.py"

    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    output_file = tmp.name

    cmd = [
        sys.executable,
        str(scraper_path),
        "--worker", output_file,
        "--headless",
        "--max-rows", "200",
        section,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent),
        )

        if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "Worker encerrou sem gravar resultado."
            return f"Erro ao acessar {section} no Teleport:\n{detail}"

        with open(output_file, encoding="utf-8") as f:
            data = json.load(f)

        if not data.get("success"):
            return f"Falha ao consultar {section}: {data.get('message', 'Erro desconhecido')}"

        dfs_data = data.get("dataframes", {})
        if not dfs_data:
            errors = data.get("errors", {})
            err_msg = "; ".join(errors.values()) if errors else "Sem dados disponíveis."
            return f"Nenhum dado encontrado em {section}. {err_msg}"

        parts = []
        for name, records in dfs_data.items():
            parts.append(
                f"**{name}** — {len(records)} registros:\n"
                f"```json\n{json.dumps(records[:100], ensure_ascii=False, indent=2, default=str)}\n```"
            )
        return "\n\n".join(parts)

    except subprocess.TimeoutExpired:
        return f"A consulta a {section} no Teleport demorou mais de 2 minutos e foi cancelada."
    except Exception as exc:
        return f"Erro ao consultar Teleport ({section}): {type(exc).__name__}: {exc}"
    finally:
        try:
            os.unlink(output_file)
        except Exception:
            pass


def execute_tool(tool_name: str, tool_input: dict) -> str:
    section = SECTION_MAP.get(tool_name)
    if section:
        return _query_teleport(section)
    return f"Ferramenta '{tool_name}' não reconhecida."


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
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block["text"])
            elif hasattr(block, "type") and block.type == "text":
                parts.append(block.text)
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
                    tool_name = block["name"]
                    # Show which section is being queried
                    section_label = {
                        "buscar_clientes": "Clientes",
                        "buscar_apolices": "Apólices",
                        "buscar_sinistros": "Sinistros",
                        "consultar_financeiro": "Financeiro",
                    }.get(tool_name, tool_name)
                    st.caption(f"🔍 Consultando {section_label} no Teleport…")

                    result = execute_tool(tool_name, block["input"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result,
                    })
            st.session_state.api_messages.append({"role": "user", "content": tool_results})
        else:
            break

    return final_text


def main():
    if "api_messages" not in st.session_state:
        st.session_state.api_messages = []

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.title("📊 Chat ERP Teleport")

        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        teleport_user = os.getenv("TELEPORT_USERNAME", "")
        teleport_pass = os.getenv("TELEPORT_PASSWORD", "")

        if api_key and teleport_user and teleport_pass:
            st.success(f"✅ Conectado como **{teleport_user}**")
        else:
            if not api_key:
                st.error("❌ ANTHROPIC_API_KEY não configurada")
            if not teleport_user or not teleport_pass:
                st.warning("⚠️ Credenciais do Teleport não configuradas")
            st.caption("Configure o arquivo `.env` com as credenciais necessárias.")

        st.divider()
        st.markdown("""
**Como usar:**
Faça perguntas em português sobre seus dados. O sistema acessa o Teleport automaticamente.

**Exemplos:**
- "Quantas apólices ativas tenho?"
- "Liste meus clientes"
- "Qual o total de comissões a receber?"
- "Mostre os sinistros em aberto"
- "Quais apólices vencem esse mês?"
        """)

        if st.session_state.api_messages:
            st.divider()
            if st.button("🔄 Nova conversa", use_container_width=True):
                st.session_state.api_messages = []
                st.rerun()

    # ── Main chat ─────────────────────────────────────────────────────────────
    st.title("💬 Chat com ERP Teleport")

    if not os.getenv("ANTHROPIC_API_KEY") or not os.getenv("TELEPORT_USERNAME"):
        st.warning("Configure o arquivo `.env` com as credenciais para começar.")
        return

    # Display conversation
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

    if prompt := st.chat_input("Faça uma pergunta sobre seus dados do Teleport…"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Consultando Teleport…"):
                response_text = process_message(prompt)
            st.markdown(response_text)


if __name__ == "__main__":
    main()
