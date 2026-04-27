"""
Scraper para o ERP Teleport (teleport.com.br).
Usa Playwright para fazer login e extrair dados via navegação web.

Configuração necessária no arquivo .env:
    TELEPORT_URL=https://teleport.com.br/0
    TELEPORT_USERNAME=seu_usuario
    TELEPORT_PASSWORD=sua_senha

Após instalar: playwright install chromium
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Callable
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEPORT_URL = os.getenv("TELEPORT_URL", "https://teleport.com.br/0")
TELEPORT_USERNAME = os.getenv("TELEPORT_USERNAME", "")
TELEPORT_PASSWORD = os.getenv("TELEPORT_PASSWORD", "")

# Section definitions: key → (display label, menu keywords for navigation)
SECTIONS: dict[str, dict] = {
    "clientes": {
        "label": "Clientes",
        "keywords": ["cliente", "segurado", "tomador"],
    },
    "apolices": {
        "label": "Apólices",
        "keywords": ["apólice", "apolice", "proposta"],
    },
    "sinistros": {
        "label": "Sinistros",
        "keywords": ["sinistro", "aviso"],
    },
    "financeiro": {
        "label": "Financeiro",
        "keywords": ["financeiro", "comissão", "comissao", "repasse", "extrato"],
    },
}


@dataclass
class ScraperResult:
    success: bool = False
    dataframes: dict[str, pd.DataFrame] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    message: str = ""


# ── DOM Helpers ────────────────────────────────────────────────────────────────

def _find_and_click_menu(page, keywords: list[str]) -> bool:
    """Clicks the first visible nav link that matches any keyword."""
    for kw in keywords:
        candidates = [
            f"nav a:text-matches('{kw}', 'i')",
            f"[role='navigation'] a:text-matches('{kw}', 'i')",
            f"[class*='menu'] a:text-matches('{kw}', 'i')",
            f"[class*='sidebar'] a:text-matches('{kw}', 'i')",
            f"[class*='nav'] a:text-matches('{kw}', 'i')",
            f"a:text-matches('{kw}', 'i')",
            f"button:text-matches('{kw}', 'i')",
        ]
        for sel in candidates:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=600):
                    el.click()
                    page.wait_for_load_state("networkidle", timeout=15_000)
                    return True
            except Exception:
                continue
    return False


def _extract_html_tables(page) -> list[pd.DataFrame]:
    """Extracts DataFrames from all <table> and role=grid elements on the page."""
    frames: list[pd.DataFrame] = []

    # ── Standard HTML <table> ─────────────────────────────────────────────────
    for tbl in page.query_selector_all("table"):
        try:
            head_els = tbl.query_selector_all("thead tr th, thead tr td")
            if not head_els:
                head_els = tbl.query_selector_all("tr:first-child th")
            if not head_els:
                head_els = tbl.query_selector_all("tr:first-child td")
            headers = [h.inner_text().strip() for h in head_els]

            body_rows = tbl.query_selector_all("tbody tr")
            if not body_rows:
                all_rows = tbl.query_selector_all("tr")
                body_rows = all_rows[1:] if len(all_rows) > 1 else []

            data = []
            for row in body_rows:
                cells = row.query_selector_all("td")
                if cells:
                    data.append([c.inner_text().strip() for c in cells])

            if not data:
                continue

            n = len(data[0])
            if len(headers) > n:
                headers = headers[:n]
            elif len(headers) < n:
                headers += [f"col_{i}" for i in range(len(headers), n)]

            df = pd.DataFrame(data, columns=headers)
            if len(df) > 0:
                frames.append(df)
        except Exception as exc:
            logger.debug("Table extraction skipped: %s", exc)

    # ── ARIA grid (ag-Grid, Kendo, Angular Material, etc.) ────────────────────
    if not frames:
        for grid in page.query_selector_all("[role='grid'], [class*='ag-root'], [class*='k-grid']"):
            try:
                headers = [
                    h.inner_text().strip()
                    for h in grid.query_selector_all("[role='columnheader']")
                ]
                rows_els = grid.query_selector_all("[role='row']")
                data = []
                for row in rows_els:
                    cells = row.query_selector_all("[role='gridcell']")
                    if cells:
                        data.append([c.inner_text().strip() for c in cells])

                if data and headers:
                    df = pd.DataFrame(data, columns=headers[: len(data[0])])
                    if len(df) > 0:
                        frames.append(df)
            except Exception:
                continue

    return frames


def _df_from_json_responses(captured: list[dict]) -> pd.DataFrame | None:
    """Tries to build a DataFrame from captured JSON API responses."""
    best: pd.DataFrame | None = None

    for entry in captured:
        body = entry.get("data")
        try:
            if isinstance(body, list) and len(body) > 0 and isinstance(body[0], dict):
                df = pd.json_normalize(body)
                if best is None or len(df) > len(best):
                    best = df
            elif isinstance(body, dict):
                for val in body.values():
                    if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                        df = pd.json_normalize(val)
                        if best is None or len(df) > len(best):
                            best = df
        except Exception:
            continue

    return best


# ── Public API ─────────────────────────────────────────────────────────────────

def scrape_teleport(
    headless: bool = True,
    sections: list[str] | None = None,
    progress_callback: Callable[[str, float], None] | None = None,
    max_rows: int = 500,
) -> ScraperResult:
    """
    Faz login no Teleport ERP e extrai dados das seções indicadas.

    Args:
        headless: Se True, executa sem abrir janela do browser.
        sections: Lista de chaves de SECTIONS a importar (None = todas).
        progress_callback: Callable(mensagem, 0.0–1.0) para atualizar progresso.
        max_rows: Limite de linhas por seção.

    Returns:
        ScraperResult com .success, .dataframes e .errors.
    """
    try:
        from playwright.sync_api import sync_playwright  # lazy import
    except ImportError:
        return ScraperResult(
            success=False,
            message=(
                "Playwright não instalado.\n"
                "Execute: pip install playwright && playwright install chromium"
            ),
        )

    username = TELEPORT_USERNAME
    password = TELEPORT_PASSWORD
    base_url = TELEPORT_URL.rstrip("/")

    if not username or not password:
        return ScraperResult(
            success=False,
            message=(
                "Credenciais não configuradas. "
                "Adicione TELEPORT_USERNAME e TELEPORT_PASSWORD ao arquivo .env"
            ),
        )

    to_scrape = [s for s in (sections or list(SECTIONS.keys())) if s in SECTIONS]
    result = ScraperResult()

    def report(msg: str, pct: float) -> None:
        logger.info(msg)
        if progress_callback:
            progress_callback(msg, min(pct, 1.0))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = browser.new_context(viewport={"width": 1366, "height": 768})
        page = ctx.new_page()

        try:
            # ── Login ──────────────────────────────────────────────────────────
            report("Conectando ao Teleport ERP…", 0.04)
            page.goto(base_url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(1_000)

            report("Preenchendo credenciais…", 0.08)

            user_selectors = [
                "input[type='email']",
                "input[name='email']",
                "input[name='login']",
                "input[name='username']",
                "input[id='email']",
                "input[id='login']",
                "input[id='username']",
                "input[placeholder*='mail']",
                "input[placeholder*='suário']",
                "input[placeholder*='ogin']",
                "input[type='text']:visible",
            ]
            filled_user = False
            for sel in user_selectors:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=500):
                        el.fill(username)
                        filled_user = True
                        break
                except Exception:
                    continue

            if not filled_user:
                result.message = "Campo de usuário não encontrado na página de login."
                return result

            try:
                pw_field = page.locator("input[type='password']").first
                pw_field.wait_for(state="visible", timeout=5_000)
                pw_field.fill(password)
            except Exception:
                result.message = "Campo de senha não encontrado na página de login."
                return result

            submit_selectors = [
                "button[type='submit']",
                "input[type='submit']",
                "button:text-matches('entrar', 'i')",
                "button:text-matches('login', 'i')",
                "button:text-matches('acessar', 'i')",
                "button:text-matches('confirmar', 'i')",
            ]
            submitted = False
            for sel in submit_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=500):
                        btn.click()
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                page.locator("input[type='password']").first.press("Enter")

            page.wait_for_load_state("networkidle", timeout=25_000)
            page.wait_for_timeout(2_000)

            # Detect login failure via visible error messages
            error_els = page.query_selector_all(
                "[class*='error']:visible, [class*='alert-danger']:visible, "
                "[class*='invalid-feedback']:visible, .error:visible, #error:visible"
            )
            if error_els:
                msg = error_els[0].inner_text().strip()
                result.message = f"Erro de login: {msg}" if msg else "Credenciais inválidas."
                return result

            body_text = page.inner_text("body").lower()
            failure_phrases = [
                "senha incorreta", "usuário não encontrado",
                "invalid credentials", "inválido", "não autorizado",
            ]
            if any(ph in body_text for ph in failure_phrases):
                result.message = "Credenciais inválidas. Verifique TELEPORT_USERNAME e TELEPORT_PASSWORD."
                return result

            report("Login realizado com sucesso!", 0.15)
            result.success = True

            # ── Scrape each section ────────────────────────────────────────────
            n = len(to_scrape)
            for i, key in enumerate(to_scrape):
                sec = SECTIONS[key]
                p0 = 0.15 + (i / n) * 0.82
                p1 = 0.15 + ((i + 1) / n) * 0.82

                report(f"Navegando para {sec['label']}…", p0)

                # Intercept JSON responses during navigation
                captured_json: list[dict] = []

                def _on_response(resp, _cap=captured_json):
                    try:
                        ct = resp.headers.get("content-type", "")
                        if resp.status == 200 and "json" in ct:
                            _cap.append({"data": resp.json(), "url": resp.url})
                    except Exception:
                        pass

                page.on("response", _on_response)

                # Try menu click first
                navigated = _find_and_click_menu(page, sec["keywords"])

                # Fallback: direct URL guesses
                if not navigated:
                    for kw in sec["keywords"]:
                        for candidate in (
                            f"{base_url}/{kw}",
                            f"{base_url}/#{kw}",
                        ):
                            try:
                                page.goto(candidate, wait_until="networkidle", timeout=10_000)
                                if kw in page.url.lower():
                                    navigated = True
                                    break
                            except Exception:
                                continue
                        if navigated:
                            break

                page.wait_for_timeout(2_000)
                page.remove_listener("response", _on_response)

                if not navigated:
                    result.errors[key] = f"Não foi possível navegar para {sec['label']}"
                    report(f"  ⚠ {sec['label']}: seção não encontrada", p1)
                    continue

                # Trigger search/list to populate data if needed
                for btn_text in ["Buscar", "Pesquisar", "Listar", "Ver todos", "Todos"]:
                    try:
                        btn = page.locator(f"button:text-matches('{btn_text}', 'i')").first
                        if btn.is_visible(timeout=600):
                            btn.click()
                            page.wait_for_timeout(2_000)
                            break
                    except Exception:
                        pass

                report(f"Extraindo dados de {sec['label']}…", (p0 + p1) / 2)

                # 1. Try JSON API data (richer, cleaner)
                df = _df_from_json_responses(captured_json)

                # 2. Fallback to HTML scraping
                if df is None or len(df) == 0:
                    tables = _extract_html_tables(page)
                    if tables:
                        df = max(tables, key=len)

                if df is not None and len(df) > 0:
                    result.dataframes[sec["label"]] = df.head(max_rows)
                    report(f"  ✓ {sec['label']}: {len(df)} registros", p1)
                else:
                    result.errors[key] = f"Nenhum dado encontrado em {sec['label']}"
                    report(f"  ⚠ {sec['label']}: dados não encontrados", p1)

            report("Importação concluída!", 1.0)

        except Exception as exc:
            result.message = f"Erro durante a importação: {exc}"
            logger.exception("Scraping error")
        finally:
            ctx.close()
            browser.close()

    return result
