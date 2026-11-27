"""Smoke e regressão das rotas e da identidade visual do SocorreAqui.

Não altera regras de negócio: apenas confirma que as páginas públicas
respondem, que as protegidas exigem login, e que a fonte oficial e os
ícones estão aplicados.
"""
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
CSS = (RAIZ / "app" / "static" / "style.css").read_text(encoding="utf-8")
BASE = (RAIZ / "app" / "templates" / "base.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("url", [
    "/",
    "/?explorar=1",
    "/comecar",
    "/cadastro",
    "/login",
    "/robots.txt",
    "/sitemap.xml",
])
def test_rotas_publicas_respondem_200(client, url):
    resposta = client.get(url)
    assert resposta.status_code == 200, url


@pytest.mark.parametrize("url", [
    "/indicar",
    "/minha-localizacao",
    "/profissional/perfil/editar",
    "/admin",
])
def test_rotas_protegidas_exigem_login(client, url):
    resposta = client.get(url, follow_redirects=False)
    assert resposta.status_code in (303, 307, 401, 403), url


def test_perfil_inexistente_redireciona_para_home(client):
    resposta = client.get("/profissional/999999", follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] in ("/", "http://testserver/")


def test_landing_carrega_lucide_sem_emoji_de_estrela(client):
    html = client.get("/").text
    assert "lucide.min.js" in html
    assert "⭐" not in html
    assert "✓" not in html


def test_fonte_oficial_e_montserrat_em_todo_o_site():
    assert "family=Montserrat" in BASE
    assert "family=Poppins" not in BASE
    assert "'Montserrat'" in CSS
    assert "'Poppins'" not in CSS


def test_alvos_de_toque_minimos_em_telas_de_toque():
    assert "@media (pointer: coarse)" in CSS
    assert "min-height: 44px" in CSS


def test_selo_indicado_tem_explicacao(client):
    # O selo só aparece com perfil indicado; o texto explicativo fica no
    # template compartilhado, verificado estaticamente aqui.
    componentes = (RAIZ / "app" / "templates" / "_componentes.html").read_text(encoding="utf-8")
    assert 'title="Perfil indicado por outro usuário.' in componentes
