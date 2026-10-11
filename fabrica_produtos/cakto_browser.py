"""Plano C da capa na Cakto: a API aceita o PUT (200) mas não grava `image` e não tem upload. Então o robô faz o que
você faria no painel: entra com e-mail/senha (conta sem 2FA), abre o produto 'Sem imagem', envia docs/capas/{id}.png
no <input type=file>, salva e CONFIRMA lendo o produto pela API (image != null).

Secrets: CAKTO_EMAIL e CAKTO_PASSWORD (sem eles o módulo sai sem erro). Os seletores do painel não são documentados:
cada passo tem alternativas e, se algo falhar, um screenshot vai para cakto-browser-debug/ (artefato do workflow).
Uso: python -m fabrica_produtos.cakto_browser
"""
import os
import re
import sys

from . import catalogo, config_fabrica as cfg, plataformas

URL_LOGIN = "https://app.cakto.com.br/login"
URL_PRODUTOS = "https://app.cakto.com.br/dashboard/products?tab=products&flow=v2"
PASTA_DEBUG = "cakto-browser-debug"
TEMPO = 30_000  # ms por espera


def pendentes(plat=None) -> list[tuple[dict, str, str]]:
    """[(produto do catálogo, id na Cakto, caminho da capa)] dos produtos que existem na Cakto sem imagem."""
    from .registrador import gerar_capa_produto
    plat = plat or plataformas.Cakto()
    out = []
    for p in catalogo.carregar()["produtos"]:
        if p.get("status") not in ("pronto", "aguardando_cadastro"):
            continue
        achado = plat.buscar_por_nome(p["nome"])
        if not achado or achado.get("imagem"):
            continue
        capa = os.path.join("docs", "capas", f"{p['id']}.png")
        if not os.path.isfile(capa):
            capa = gerar_capa_produto(p, [])
        out.append((p, achado["id"], capa))
    return out


def _primeiro_visivel(page, candidatos, tempo=TEMPO):
    """Primeiro locator da lista que aparece; None se nenhum aparecer."""
    for loc in candidatos:
        try:
            loc.first.wait_for(state="visible", timeout=max(tempo // len(candidatos), 3000))
            return loc.first
        except Exception:
            continue
    return None


def entrar(page, email: str, senha: str) -> None:
    page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=TEMPO)
    campo_email = _primeiro_visivel(page, [page.locator("input[type=email]"), page.locator("input[name=email]"),
                                           page.get_by_label(re.compile("e-?mail", re.I))])
    if campo_email is None:
        raise RuntimeError("campo de e-mail não apareceu na tela de login")
    campo_email.fill(email)
    page.locator("input[type=password]").first.fill(senha)
    botao = _primeiro_visivel(page, [page.get_by_role("button", name=re.compile(r"entrar|login|acessar", re.I)),
                                     page.locator("button[type=submit]")], 10_000)
    if botao is None:
        page.locator("input[type=password]").first.press("Enter")
    else:
        botao.click()
    page.wait_for_url(lambda u: "/login" not in u, timeout=TEMPO)


def abrir_edicao(page, nome: str, cakto_id: str) -> None:
    """Lista de produtos > busca pelo nome > abre o produto > Editar produto (se existir o botão)."""
    page.goto(URL_PRODUTOS, wait_until="domcontentloaded", timeout=TEMPO)
    busca = _primeiro_visivel(page, [page.get_by_placeholder(re.compile(r"buscar|pesquis|search", re.I)),
                                     page.locator("input[type=search]")], 10_000)
    if busca is not None:
        busca.fill(nome[:40])
        page.wait_for_timeout(1500)
    item = _primeiro_visivel(page, [page.get_by_text(nome, exact=False), page.locator(f"a[href*='{cakto_id}']")])
    if item is None:
        page.goto(f"https://app.cakto.com.br/dashboard/products/{cakto_id}", wait_until="domcontentloaded", timeout=TEMPO)
    else:
        item.click()
    editar = _primeiro_visivel(page, [page.get_by_role("button", name=re.compile(r"editar produto", re.I)),
                                      page.get_by_role("link", name=re.compile(r"editar produto", re.I)),
                                      page.get_by_text(re.compile(r"editar produto", re.I))], 10_000)
    if editar is not None:
        editar.click()


def enviar_capa(page, capa: str) -> None:
    entrada = page.locator("input[type=file]")
    entrada.first.wait_for(state="attached", timeout=TEMPO)
    entrada.first.set_input_files(capa)
    page.wait_for_timeout(2000)  # o painel pré-visualiza/envia o arquivo
    salvar = _primeiro_visivel(page, [page.get_by_role("button", name=re.compile(r"^\s*salvar", re.I)),
                                      page.get_by_text(re.compile(r"^\s*salvar", re.I))])
    if salvar is None:
        raise RuntimeError("botão Salvar não encontrado")
    salvar.click()
    page.wait_for_timeout(3000)


def confirmar(plat, cakto_id: str) -> str:
    return plat._req("GET", f"/public_api/products/{cakto_id}/").get("image") or ""


def _debug(page, produto_id: str) -> None:
    try:
        os.makedirs(PASTA_DEBUG, exist_ok=True)
        page.screenshot(path=os.path.join(PASTA_DEBUG, f"{produto_id}.png"), full_page=True)
    except Exception:
        pass


def processar(page, plat, itens) -> dict[str, bool]:
    """Um produto por vez; a falha de um não impede os outros. Cada resultado vira uma linha CAKTO_BROWSER_UPLOAD."""
    res = {}
    for p, cakto_id, capa in itens:
        try:
            abrir_edicao(page, p["nome"], cakto_id)
            enviar_capa(page, capa)
            imagem = confirmar(plat, cakto_id)
            ok = bool(imagem)
            print(f"CAKTO_BROWSER_UPLOAD {p['id']} {'OK' if ok else 'FALHOU: salvou mas a API ainda mostra image=null'}",
                  flush=True)
        except Exception as e:  # noqa: BLE001 - o painel pode mudar de qualquer jeito; registra e segue
            ok = False
            print(f"CAKTO_BROWSER_UPLOAD {p['id']} FALHOU: {type(e).__name__}: {str(e)[:300]}", flush=True)
            _debug(page, p["id"])
        res[p["id"]] = ok
    return res


def main() -> int:
    email, senha = os.getenv("CAKTO_EMAIL", "").strip(), os.getenv("CAKTO_PASSWORD", "")
    if not email or not senha:
        print("CAKTO_BROWSER: CAKTO_EMAIL/CAKTO_PASSWORD não definidos; nada a fazer.")
        return 0
    plat = plataformas.Cakto()
    if not plat.configurada():
        print("CAKTO_BROWSER: faltam CAKTO_CLIENT_ID/CAKTO_CLIENT_SECRET para conferir o resultado; nada a fazer.")
        return 0
    itens = pendentes(plat)
    if not itens:
        print("CAKTO_BROWSER: nenhum produto 'Sem imagem'.")
        return 0
    print(f"CAKTO_BROWSER: {len(itens)} produto(s) sem imagem.")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(headless=True)
        page = navegador.new_context(viewport={"width": 1366, "height": 900}, locale="pt-BR").new_page()
        try:
            entrar(page, email, senha)
        except Exception as e:  # noqa: BLE001
            print(f"CAKTO_BROWSER_LOGIN FALHOU: {type(e).__name__}: {str(e)[:300]}", flush=True)
            _debug(page, "login")
            navegador.close()
            return 1
        print("CAKTO_BROWSER_LOGIN OK", flush=True)
        res = processar(page, plat, itens)
        navegador.close()
    falhas = [i for i, ok in res.items() if not ok]
    print(f"CAKTO_BROWSER: {len(res) - len(falhas)} ok, {len(falhas)} falha(s).")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
