"""
Atualização automática dos dados do Teleport ERP.

Uso manual:
    python atualizar_dados.py

Agendamento no Windows (Task Scheduler):
    Ação: python C:\\caminho\\para\\acessosERPs\\atualizar_dados.py
    Gatilho: Diariamente às 07:00

O script faz login no Teleport, exporta os relatórios de Produção e
Clientes, e salva na pasta data/ — que o app.py carrega automaticamente.
"""

import os
import sys
import time
import shutil
import logging
import tempfile
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

LOG_FILE = Path(__file__).parent / "atualizar_dados.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

TELEPORT_URL = os.getenv("TELEPORT_URL", "https://teleport.com.br/0")
TELEPORT_USERNAME = os.getenv("TELEPORT_USERNAME", "")
TELEPORT_PASSWORD = os.getenv("TELEPORT_PASSWORD", "")

# Seções a exportar: (nome_arquivo_destino, texto_menu, texto_botao_exportar)
EXPORTS = [
    ("producao",  "Produção",           ["Exportar", "Excel", "XLS", "Download"]),
    ("clientes",  "Pesquisar Clientes", ["Exportar", "Excel", "XLS", "Download"]),
]


def _safe_wait(page, ms: int = 2000) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=ms)
    except Exception:
        pass
    page.wait_for_timeout(1000)


def run_export() -> dict[str, bool]:
    """
    Faz login no Teleport, navega para cada seção e tenta exportar XLS.
    Retorna {nome: True/False} indicando sucesso por seção.
    """
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        asyncio.set_event_loop(asyncio.ProactorEventLoop())

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright não instalado. Execute: pip install playwright && playwright install chromium")
        return {}

    results: dict[str, bool] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            downloads_path=str(DATA_DIR),
        )
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 768},
            accept_downloads=True,
        )
        page = ctx.new_page()

        try:
            # ── Login ──────────────────────────────────────────────────────
            log.info("Conectando ao Teleport...")
            page.goto(TELEPORT_URL, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(2_000)

            for sel in ["input[type='email']", "input[name='login']", "input[type='text']:visible"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=800):
                        el.fill(TELEPORT_USERNAME)
                        break
                except Exception:
                    continue

            pw_field = page.locator("input[type='password']").first
            pw_field.fill(TELEPORT_PASSWORD)

            for sel in ["button[type='submit']", "button:text-matches('entrar', 'i')", "button:text-matches('login', 'i')"]:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=500):
                        btn.click()
                        break
                except Exception:
                    continue

            page.wait_for_timeout(4_000)
            log.info(f"Login OK — URL: {page.url}")

            # ── Exportar cada seção ────────────────────────────────────────
            for nome, menu_text, export_btns in EXPORTS:
                log.info(f"Navegando para: {menu_text}")
                try:
                    el = page.locator(f"text='{menu_text}'").first
                    if el.is_visible(timeout=2_000):
                        el.click()
                        page.wait_for_timeout(3_000)
                    else:
                        log.warning(f"  Menu '{menu_text}' não encontrado")
                        results[nome] = False
                        continue
                except Exception as e:
                    log.warning(f"  Erro ao navegar: {e}")
                    results[nome] = False
                    continue

                # Clicar em Buscar para carregar todos os registros
                for btn_text in ["Buscar", "Pesquisar", "Listar"]:
                    try:
                        btn = page.locator(f"button:text-matches('{btn_text}', 'i')").first
                        if btn.is_visible(timeout=800):
                            btn.click()
                            page.wait_for_timeout(3_000)
                            break
                    except Exception:
                        pass

                # Tentar exportar
                exported = False
                for btn_text in export_btns:
                    try:
                        btn = page.locator(f"button:text-matches('{btn_text}', 'i'), a:text-matches('{btn_text}', 'i')").first
                        if btn.is_visible(timeout=800):
                            with page.expect_download(timeout=30_000) as dl:
                                btn.click()
                            download = dl.value
                            dest = DATA_DIR / f"{nome}_raw{Path(download.suggested_filename).suffix or '.xlsx'}"
                            download.save_as(str(dest))
                            log.info(f"  ✓ {nome}: baixado → {dest.name}")
                            exported = True
                            break
                    except Exception:
                        continue

                if not exported:
                    # Fallback: salva screenshot para diagnóstico
                    page.screenshot(path=str(DATA_DIR / f"debug_{nome}.png"))
                    log.warning(f"  ⚠ {nome}: botão de exportar não encontrado (screenshot salvo)")
                    results[nome] = False
                else:
                    results[nome] = True

        except Exception as e:
            log.error(f"Erro durante exportação: {e}")
        finally:
            ctx.close()
            browser.close()

    return results


def convert_to_parquet() -> None:
    """Converte arquivos XLS/CSV baixados para parquet (leitura rápida pelo app)."""
    import pandas as pd

    for f in DATA_DIR.glob("*_raw.*"):
        stem = f.stem.replace("_raw", "")
        dest = DATA_DIR / f"{stem}.parquet"
        try:
            if f.suffix.lower() in (".xlsx", ".xls"):
                df = pd.read_excel(f)
            elif f.suffix.lower() == ".csv":
                df = pd.read_csv(f, sep=None, engine="python", encoding="latin-1")
            else:
                continue
            df.to_parquet(dest, index=False)
            log.info(f"Convertido: {f.name} → {dest.name} ({len(df):,} registros)")
        except Exception as e:
            log.error(f"Erro ao converter {f.name}: {e}")


if __name__ == "__main__":
    log.info("=" * 50)
    log.info(f"Início da atualização: {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    if not TELEPORT_USERNAME or not TELEPORT_PASSWORD:
        log.error("Credenciais não configuradas. Verifique o arquivo .env")
        sys.exit(1)

    results = run_export()

    if any(results.values()):
        convert_to_parquet()
        log.info("Conversão para parquet concluída.")
    else:
        log.warning("Nenhum arquivo foi exportado com sucesso.")

    log.info(f"Fim da atualização: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    log.info("=" * 50)
