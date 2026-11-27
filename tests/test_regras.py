"""Regras de negócio do SocorreAqui 2.0: publicação, visibilidade pública,
login com destino preservado, migração de categoria, páginas institucionais
e ordenação por relevância.

Usa o banco SQLite temporário definido em conftest.py. Nenhum dado real é tocado.
"""
import itertools

import pytest
from sqlalchemy import text

from app import auth, models
from app.database import SessionLocal, engine
from app.main import _corrigir_nomes_categorias

_contador = itertools.count(1)
SENHA = "Senha-Teste-2026"


def _proximo():
    return next(_contador)


def _criar_usuario(db, tipo="profissional", senha=SENHA):
    n = _proximo()
    usuario = models.User(
        nome=f"Pessoa Teste {n}",
        email=f"pessoa{n}@example.com",
        telefone=f"+55229{n:08d}",
        tipo=tipo,
        email_verificado=True,
        ativo=True,
        senha_hash=auth.gerar_hash_senha(senha),
    )
    db.add(usuario)
    db.flush()
    return usuario


def _categoria(db, nome=None):
    nome = nome or f"Categoria Teste {_proximo()}"
    cat = db.query(models.Category).filter(models.Category.nome == nome).first()
    if not cat:
        cat = models.Category(nome=nome, grupo="Outros")
        db.add(cat)
        db.flush()
    return cat


def _criar_perfil(db, *, completo=True, aprovado=True, descricao="Serviço de teste.", **extra):
    usuario = _criar_usuario(db)
    if not completo:
        # Sem WhatsApp válido: o telefone do cadastro também fica inválido,
        # porque ele serve de fallback para o WhatsApp.
        usuario.telefone = f"9{usuario.id:03d}"
    perfil = models.ProfessionalProfile(
        usuario_id=usuario.id,
        cidade=extra.pop("cidade", "Macaé"),
        estado=extra.pop("estado", "RJ"),
        descricao=descricao if completo else "",
        aprovado=aprovado,
        ativo=True,
        whatsapp=extra.pop("whatsapp", "22999990000") if completo else None,
        **extra,
    )
    if completo:
        perfil.categorias = [_categoria(db)]
    db.add(perfil)
    db.flush()
    return perfil


@pytest.fixture
def db():
    sessao = SessionLocal()
    try:
        yield sessao
        sessao.commit()
    finally:
        sessao.close()


# --- Regra de publicação --------------------------------------------------

def test_perfil_completo_e_publicavel(db):
    perfil = _criar_perfil(db)
    assert perfil.pendencias_publicacao == []
    assert perfil.publicavel is True


def test_perfil_incompleto_lista_o_que_falta(db):
    perfil = _criar_perfil(db, completo=False)
    faltas = perfil.pendencias_publicacao
    assert "Categoria principal" in faltas
    assert "WhatsApp válido" in faltas
    assert "Descrição dos serviços" in faltas
    assert perfil.publicavel is False


@pytest.mark.parametrize("numero, valido", [
    ("22999990000", True),        # DDD + 9 dígitos
    ("2299999000", True),         # DDD + 8 dígitos (fixo)
    ("+5522999990000", True),     # com código do país
    ("999990000", False),         # sem DDD
    ("", False),
])
def test_whatsapp_valido(db, numero, valido):
    perfil = _criar_perfil(db, completo=False)
    perfil.usuario.telefone = f"9{_proximo()}"  # sem fallback válido e único
    perfil.whatsapp = numero
    assert perfil.whatsapp_valido is valido


# --- Visibilidade pública -------------------------------------------------

def test_perfil_incompleto_nao_aparece_no_catalogo(client, db):
    incompleto = _criar_perfil(db, completo=False)
    db.commit()
    html = client.get("/?explorar=1").text
    assert f'href="/profissional/{incompleto.id}"' not in html


def test_perfil_incompleto_retorna_redirect_para_visitante(client, db):
    incompleto = _criar_perfil(db, completo=False)
    db.commit()
    resposta = client.get(f"/profissional/{incompleto.id}", follow_redirects=False)
    assert resposta.status_code == 303


def test_perfil_completo_aparece_no_catalogo(client, db):
    completo = _criar_perfil(db, completo=True)
    db.commit()
    html = client.get("/?explorar=1").text
    assert f'href="/profissional/{completo.id}"' in html


def test_perfil_publico_nao_expoe_telefone_para_visitante(client, db):
    perfil = _criar_perfil(db, completo=True)
    telefone = perfil.usuario.telefone
    db.commit()
    html = client.get(f"/profissional/{perfil.id}").text
    assert telefone not in html
    assert "wa.me/" not in html


def test_landing_ver_perfil_abre_perfil_sem_passar_por_cadastro(client, db):
    perfil = _criar_perfil(db, completo=True, descricao="Destaque para landing.")
    db.commit()
    html = client.get("/").text
    assert f'href="/profissional/{perfil.id}"' in html
    assert f"next=%2Fprofissional%2F{perfil.id}" not in html


# --- Login preservando o destino ------------------------------------------

def test_botao_entrar_no_perfil_preserva_destino(client, db):
    perfil = _criar_perfil(db, completo=True)
    db.commit()
    import re

    html = client.get(f"/profissional/{perfil.id}").text
    links = re.findall(r'href="(/comecar[^"]*)"', html)
    esperado = f"next=/profissional/{perfil.id}"
    assert any(esperado in link.replace("&amp;", "&") for link in links), links


def test_dono_ve_perfil_incompleto_e_recebe_lista_de_pendencias(client, db):
    perfil = _criar_perfil(db, completo=False)
    dono_email = perfil.usuario.email
    db.commit()

    resposta = client.post(
        "/login", data={"email": dono_email, "senha": SENHA, "next": ""}, follow_redirects=False
    )
    assert resposta.status_code == 303

    publico = client.get(f"/profissional/{perfil.id}", follow_redirects=False)
    assert publico.status_code == 200
    editar = client.get("/profissional/perfil/editar")
    assert editar.status_code == 200
    assert "ainda não aparece no catálogo público" in editar.text
    assert "Categoria principal" in editar.text

    client.get("/logout")


# --- Migração do nome da categoria ----------------------------------------

def test_migracao_corrige_nome_sem_trocar_id(db):
    nome_antigo = "Serviços domésticoa"
    nome_novo = "Serviços domésticos"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM categories WHERE nome IN (:a, :b)"), {"a": nome_antigo, "b": nome_novo})
        conn.execute(text("INSERT INTO categories (nome, grupo) VALUES (:n, 'Casa e Reformas')"), {"n": nome_antigo})
        id_antigo = conn.execute(text("SELECT id FROM categories WHERE nome = :n"), {"n": nome_antigo}).scalar()

    _corrigir_nomes_categorias()

    with engine.begin() as conn:
        assert conn.execute(text("SELECT id FROM categories WHERE nome = :n"), {"n": nome_novo}).scalar() == id_antigo
        assert conn.execute(text("SELECT COUNT(*) FROM categories WHERE nome = :n"), {"n": nome_antigo}).scalar() == 0


def test_migracao_e_idempotente(db):
    _corrigir_nomes_categorias()
    _corrigir_nomes_categorias()
    with engine.begin() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM categories WHERE nome = 'Serviços domésticos'")).scalar()
    assert total <= 1


# --- Páginas institucionais -----------------------------------------------

@pytest.mark.parametrize("url, trecho", [
    ("/sobre", "Sobre o SocorreAqui"),
    ("/contato", "Canais de contato"),
    ("/termos", "Termos de uso"),
    ("/privacidade", "Política de privacidade"),
])
def test_paginas_institucionais_respondem(client, url, trecho):
    resposta = client.get(url)
    assert resposta.status_code == 200
    assert trecho in resposta.text


def test_minutas_mostram_aviso_de_revisao(client):
    assert "Minuta em revisão" in client.get("/termos").text
    assert "Minuta em revisão" in client.get("/privacidade").text
    assert "Minuta em revisão" not in client.get("/contato").text


def test_institucionais_nao_inventam_dados_institucionais(client):
    texto = client.get("/contato").text
    assert "CNPJ" in texto and "a informar" in texto


# --- Relevância ------------------------------------------------------------

def test_relevancia_coloca_quem_bate_no_nome_antes(client, db):
    # Os dois aparecem na busca pela descrição; só um tem o termo no nome.
    alvo = _criar_perfil(db, completo=True, descricao="Atendo encanamento residencial.")
    alvo.usuario.nome = "Ana Encanamento Teste"
    outro = _criar_perfil(db, completo=True, descricao="Também faço encanamento.")
    outro.usuario.nome = "Bruno Geral Teste"
    db.commit()

    html = client.get("/?busca=encanamento&ordenar=relevancia").text
    posicao_alvo = html.find(f'href="/profissional/{alvo.id}"')
    posicao_outro = html.find(f'href="/profissional/{outro.id}"')
    assert posicao_alvo != -1 and posicao_outro != -1
    assert posicao_alvo < posicao_outro


def test_resultados_expoem_localizacao_unica_e_relevancia(client):
    html = client.get("/?explorar=1").text
    assert html.count('id="btnCheckin"') == 1
    assert "Usar minha localização" in html
    assert "Relevância" in html
