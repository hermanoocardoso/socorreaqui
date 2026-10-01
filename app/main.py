"""
Aplicação principal.

Para rodar localmente:

    uvicorn app.main:app --reload

Depois abra http://127.0.0.1:8000 no navegador.

Veja o LEIA-ME.md na raiz do projeto para o passo a passo completo
de instalação.
"""
import hashlib
import io
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, Depends, Form, UploadFile, File
from fastapi.responses import RedirectResponse, PlainTextResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from app.database import Base, engine, get_db
from app import models, auth, storage, email_utils
from app.localidades import UFS_BRASIL
from app.oauth import oauth, google_oauth_habilitado
from app.seed import rodar_seed

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Cria as tabelas automaticamente se ainda não existirem, e mantém as
# categorias padrão (nome + grupo) sempre alinhadas com CATEGORIAS_PADRAO —
# assim um novo deploy já atualiza o catálogo de categorias sozinho, sem
# precisar rodar "python -m app.seed" manualmente em produção.
Base.metadata.create_all(bind=engine)


def _garantir_colunas_novas():
    """create_all() só cria tabelas que ainda não existem -- não adiciona
    coluna nova numa tabela que já existe em produção. Como não usamos uma
    ferramenta de migração (Alembic), essa função confere colunas que os
    modelos esperam e adiciona na mão as que estiverem faltando, sem apagar
    nada. Precisa ficar em dia manualmente sempre que um Column novo for
    adicionado a um model existente."""
    from sqlalchemy import inspect, text

    inspetor = inspect(engine)
    if "professional_profiles" not in inspetor.get_table_names():
        return  # tabela acabou de ser criada pelo create_all, já vem completa
    colunas = {c["name"] for c in inspetor.get_columns("professional_profiles")}
    if "criado_via_indicacao" not in colunas:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE professional_profiles "
                "ADD COLUMN criado_via_indicacao BOOLEAN NOT NULL DEFAULT FALSE"
            ))
    if "foto_e_logo" not in colunas:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE professional_profiles "
                "ADD COLUMN foto_e_logo BOOLEAN NOT NULL DEFAULT FALSE"
            ))
    if "tipo_perfil" not in colunas:
        # Todo perfil que já existe é de prestador de serviço -- quem for
        # empresa/administração da plataforma é marcado depois, na mão.
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE professional_profiles "
                "ADD COLUMN tipo_perfil VARCHAR(20) NOT NULL DEFAULT 'professional'"
            ))
    if "estado" not in colunas:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE professional_profiles ADD COLUMN estado VARCHAR(2)"))
            # Todo cadastro anterior a esse campo é de Macaé/RJ (única cidade
            # atendida até aqui) -- preenche pra não deixar profissional
            # já aprovado sumindo do filtro por estado quando ele existir.
            conn.execute(text(
                "UPDATE professional_profiles SET estado = 'RJ' WHERE cidade IS NOT NULL AND cidade != ''"
            ))

    colunas_users = {c["name"] for c in inspetor.get_columns("users")}
    if "estado" not in colunas_users:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN estado VARCHAR(2)"))
            conn.execute(text(
                "UPDATE users SET estado = 'RJ' WHERE cidade IS NOT NULL AND cidade != ''"
            ))
    if "notificacoes_vistas_em" not in colunas_users:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN notificacoes_vistas_em TIMESTAMP"))
    if "ultimo_login_em" not in colunas_users:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN ultimo_login_em TIMESTAMP"))

    if "indicacoes" in inspetor.get_table_names():
        colunas_indicacoes = {c["name"] for c in inspetor.get_columns("indicacoes")}
        if "estado" not in colunas_indicacoes:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE indicacoes ADD COLUMN estado VARCHAR(2)"))
                conn.execute(text(
                    "UPDATE indicacoes SET estado = 'RJ' WHERE cidade IS NOT NULL AND cidade != ''"
                ))


_garantir_colunas_novas()
rodar_seed()

app = FastAPI(title="SocorreAqui")

# Chave usada para assinar o cookie de sessão. Em produção, defina a
# variável de ambiente SECRET_KEY com um valor aleatório e secreto -- sem
# isso, qualquer pessoa que veja este código (é público no GitHub) consegue
# forjar um cookie de sessão válido pra qualquer usuário, sem senha nenhuma.
_SECRET_KEY_PADRAO = "troque-esta-chave-antes-de-colocar-no-ar"
SECRET_KEY = os.getenv("SECRET_KEY", _SECRET_KEY_PADRAO)
if SECRET_KEY == _SECRET_KEY_PADRAO:
    print(
        "!" * 70 + "\n"
        "AVISO DE SEGURANÇA: SECRET_KEY não configurada -- rodando com a "
        "chave padrão, que é pública no código-fonte. Defina a variável de "
        "ambiente SECRET_KEY com um valor aleatório antes de expor este "
        "app pra internet, senão qualquer pessoa consegue forjar login.\n"
        + "!" * 70
    )

# RENDER=true é definido automaticamente pelo próprio Render em produção --
# usamos isso pra só marcar o cookie de sessão como "só HTTPS" (Secure) lá,
# já que em desenvolvimento local (http://127.0.0.1) isso impediria o login
# de funcionar sem HTTPS.
RODANDO_EM_PRODUCAO = os.getenv("RENDER", "").lower() == "true"
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    https_only=RODANDO_EM_PRODUCAO,
)


# --- Limite de tentativas por ação (proteção simples contra força bruta e
# spam) --- Guardado em memória (zera a cada reinício/deploy) -- não é
# perfeito, mas barra o ataque mais óbvio: bater a mesma ação sem parar.
# Usado pro login (senha atrás de senha), pro cadastro (spam de contas /
# flood de e-mail pro admin) e pro esqueci-minha-senha (flood de e-mail pra
# vítima).
_tentativas_por_acao: dict[str, dict[str, list[float]]] = {}


def _ip_cliente(request: Request) -> str:
    # Render (como qualquer hospedagem atrás de proxy) entrega o IP real do
    # visitante em X-Forwarded-For -- sem isso, request.client.host seria
    # sempre o IP interno do proxy, e todo mundo cairia no mesmo balde.
    encaminhado = request.headers.get("x-forwarded-for", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return request.client.host if request.client else "desconhecido"


def _acao_bloqueada(acao: str, chave: str, limite: int, janela_segundos: int) -> bool:
    agora = time.time()
    tentativas_da_acao = _tentativas_por_acao.setdefault(acao, {})
    tentativas = [t for t in tentativas_da_acao.get(chave, []) if agora - t < janela_segundos]
    tentativas_da_acao[chave] = tentativas
    return len(tentativas) >= limite


def _registrar_tentativa(acao: str, chave: str) -> None:
    _tentativas_por_acao.setdefault(acao, {}).setdefault(chave, []).append(time.time())


def _limpar_tentativas(acao: str, chave: str) -> None:
    _tentativas_por_acao.get(acao, {}).pop(chave, None)


LOGIN_MAX_TENTATIVAS = 8
LOGIN_JANELA_SEGUNDOS = 15 * 60  # 15 minutos
CADASTRO_MAX_TENTATIVAS = 5
CADASTRO_JANELA_SEGUNDOS = 30 * 60  # 30 minutos
RESET_SENHA_MAX_TENTATIVAS = 5
RESET_SENHA_JANELA_SEGUNDOS = 60 * 60  # 1 hora
REENVIO_CONFIRMACAO_MAX_TENTATIVAS = 5
REENVIO_CONFIRMACAO_JANELA_SEGUNDOS = 60 * 60  # 1 hora


def _login_bloqueado(chave: str) -> bool:
    return _acao_bloqueada("login", chave, LOGIN_MAX_TENTATIVAS, LOGIN_JANELA_SEGUNDOS)


def _registrar_tentativa_falha(chave: str) -> None:
    _registrar_tentativa("login", chave)


@app.middleware("http")
async def cabecalhos_de_seguranca(request: Request, call_next):
    """Cabeçalhos básicos de proteção do navegador -- não substituem outras
    práticas, mas custam nada e bloqueiam classes inteiras de ataque
    (clickjacking, MIME sniffing, vazamento de referrer)."""
    resposta = await call_next(request)
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["X-Frame-Options"] = "DENY"
    resposta.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if RODANDO_EM_PRODUCAO:
        resposta.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return resposta

# Quem entra no painel /admin. Aceita MAIS DE UM e-mail, separados por
# vírgula -- a mesma pessoa costuma ter uma conta pessoa física (que só
# administra o site) e uma conta empresa (que também fica no catálogo
# recebendo propostas), e as duas precisam abrir o painel. Ex:
#
#     ADMIN_EMAIL=fulano@gmail.com,empresa@gmail.com
#
# Ser admin é só permissão de painel: não muda o tipo da conta nem tira
# ninguém do catálogo. Quem decide isso é o campo `tipo` do usuário.
ADMIN_EMAILS = [
    parte.strip().lower()
    for parte in os.getenv("ADMIN_EMAIL", "").split(",")
    if parte.strip()
]


def _eh_email_admin(email: str | None) -> bool:
    return bool(email) and email.strip().lower() in ADMIN_EMAILS


# Dono da plataforma: um nível acima de admin, só pra quem realmente é dono
# do negócio (hoje só você). Admin comum (ex: a H2 Sistemas) continua com
# acesso normal ao /admin, mas NÃO vê o /dono -- números consolidados,
# gráfico de crescimento e a lista completa de usuários com filtro/busca.
# Se DONO_EMAIL não estiver configurado, cai no primeiro e-mail de
# ADMIN_EMAIL (hoje é hermanoocardoso@gmail.com, o primeiro da lista).
DONO_EMAIL = os.getenv("DONO_EMAIL", "").strip().lower() or (ADMIN_EMAILS[0] if ADMIN_EMAILS else "")


def _eh_email_dono(email: str | None) -> bool:
    return bool(email) and bool(DONO_EMAIL) and email.strip().lower() == DONO_EMAIL


def _avisar_admin_novo_cadastro(usuario: "models.User") -> None:
    # Se ADMIN_EMAIL não estiver configurado, ou o SendGrid não estiver
    # configurado, enviar_email() já cuida de não quebrar nada (só imprime
    # no terminal) -- então não precisa checar sendgrid_habilitado aqui.
    for destinatario in ADMIN_EMAILS:
        email_utils.enviar_email_novo_cadastro(
            destinatario, usuario.nome, usuario.email, usuario.telefone, usuario.tipo
        )


BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# "Cache busting" do CSS: gera um código a partir do conteúdo do arquivo, pra
# usar como ?v=... no link do style.css. Assim, toda vez que o visual muda,
# o navegador (e qualquer CDN na frente, tipo a que o Render usa) é obrigado
# a baixar a versão nova em vez de continuar usando uma antiga guardada.
with open(os.path.join(BASE_DIR, "static", "style.css"), "rb") as _f:
    CSS_VERSION = hashlib.md5(_f.read()).hexdigest()[:8]
templates.env.globals["css_version"] = CSS_VERSION
templates.env.globals["ano_atual"] = datetime.now().year
templates.env.globals["rodando_em_producao"] = RODANDO_EM_PRODUCAO
templates.env.globals["ufs_brasil"] = UFS_BRASIL


def _tojson(valor):
    # Jinja2 "puro" (sem Flask) não vem com filtro tojson — usado pra
    # embutir listas simples (ex: nomes de categoria) num <script> inline
    # com segurança, evitando fechar a tag </script> sem querer.
    return Markup(json.dumps(valor, ensure_ascii=False).replace("</", "<\\/"))


templates.env.filters["tojson"] = _tojson


def _nota_br(valor) -> str:
    """Nota no formato brasileiro: 5.0 -> "5,0". Nunca inventa nota: quem
    não tem avaliação não passa por aqui (o template checa antes)."""
    try:
        return f"{float(valor):.1f}".replace(".", ",")
    except (TypeError, ValueError):
        return ""


templates.env.filters["nota_br"] = _nota_br


def _texto_avaliacoes(total: int) -> str:
    """"1 avaliação" / "27 avaliações" -- singular e plural corretos, com o
    número real que existe no banco."""
    return "1 avaliação" if total == 1 else f"{total} avaliações"


templates.env.globals["texto_avaliacoes"] = _texto_avaliacoes

# Ícone do WhatsApp (SVG inline) usado nos botões de contato — evita
# depender de um pacote de ícones externo pra um único glifo.
_WHATSAPP_ICON_PATH = (
    "M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.75.46 3.45 1.32 4.95L2 22l5.25-1.38c1.45.79 3.08 1.21 4.79 "
    "1.21h.01c5.46 0 9.9-4.45 9.9-9.91C21.95 6.45 17.5 2 12.04 2zm0 18.15h-.01c-1.5 0-2.97-.4-4.25-1.16l-.3-"
    ".18-3.12.82.83-3.04-.2-.31a8.19 8.19 0 0 1-1.26-4.37c0-4.54 3.7-8.24 8.25-8.24 2.2 0 4.27.86 5.83 2.42a"
    "8.18 8.18 0 0 1 2.41 5.83c0 4.55-3.7 8.23-8.18 8.23zm4.52-6.16c-.25-.12-1.47-.72-1.7-.81-.23-.08-.4-.12"
    "-.56.13-.17.25-.64.81-.78.97-.14.17-.29.19-.54.06-.25-.12-1.04-.38-1.98-1.22-.73-.65-1.23-1.46-1.37-1.7"
    "1-.14-.25-.02-.38.11-.51.11-.11.25-.29.37-.43.12-.14.16-.25.25-.41.08-.17.04-.31-.02-.43-.06-.12-.56-1."
    "36-.77-1.86-.2-.48-.41-.42-.56-.43h-.48c-.17 0-.43.06-.66.31-.23.25-.86.85-.86 2.07 0 1.22.89 2.4 1.01 "
    "2.57.12.17 1.75 2.68 4.25 3.75.59.26 1.06.41 1.42.53.6.19 1.14.16 1.57.1.48-.07 1.47-.6 1.68-1.18.21-.5"
    "8.21-1.08.14-1.18-.06-.1-.23-.16-.48-.28z"
)


def _whatsapp_icon(tamanho: int = 18) -> Markup:
    return Markup(
        f'<svg width="{tamanho}" height="{tamanho}" viewBox="0 0 24 24" fill="currentColor" '
        f'aria-hidden="true"><path d="{_WHATSAPP_ICON_PATH}"/></svg>'
    )


templates.env.globals["whatsapp_icon"] = _whatsapp_icon

# Formatos de foto aceitos (chave = content-type enviado pelo navegador).
# Usamos a extensão daqui em vez de confiar no nome do arquivo enviado.
EXTENSOES_FOTO_PERMITIDAS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_FORMATO_PILLOW_POR_CONTENT_TYPE = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
TAMANHO_MAXIMO_FOTO = 5 * 1024 * 1024  # 5 MB


def _validar_e_normalizar_foto(conteudo: bytes, content_type: str) -> bytes | None:
    """Confere que os bytes enviados são mesmo uma imagem de verdade (o
    content-type do formulário é só o que o navegador declarou, e isso o
    usuário controla -- não dá pra confiar nele sozinho) e reexporta a
    imagem do zero, o que também descarta metadados EXIF (ex: coordenadas
    de GPS de onde a foto foi tirada, que o profissional provavelmente não
    quer expor). Retorna None se o arquivo não for uma imagem válida.
    """
    from PIL import Image, ImageOps

    try:
        imagem = Image.open(io.BytesIO(conteudo))
        imagem.verify()
        # verify() invalida o objeto pra qualquer uso seguinte -- reabre.
        imagem = Image.open(io.BytesIO(conteudo))
        imagem = ImageOps.exif_transpose(imagem)  # aplica a rotação do EXIF antes de descartá-lo
        formato = _FORMATO_PILLOW_POR_CONTENT_TYPE[content_type]
        if formato == "JPEG" and imagem.mode in ("RGBA", "P", "LA"):
            imagem = imagem.convert("RGB")
        buffer = io.BytesIO()
        imagem.save(buffer, format=formato)
        return buffer.getvalue()
    except Exception:
        return None

def _imagem_tem_transparencia(conteudo: bytes) -> bool:
    """Diz se a imagem tem fundo transparente.

    Isso não é palpite, é fato: foto de pessoa nunca vem com o fundo
    recortado, então transparência quer dizer logomarca. Importa pro
    enquadramento na tela -- imagem transparente PRECISA aparecer inteira,
    porque cortar tira parte da marca e o fundo vazado deixa ver o que
    está atrás.

    Tentei também adivinhar o resto (logo em JPEG com fundo branco, pelos
    cantos de cor chapada), mas nas imagens reais do site isso marcava foto
    de pessoa como logomarca -- por isso os casos ambíguos ficam na
    caixinha "é uma logomarca" do formulário, em vez de virar chute.
    """
    from PIL import Image

    try:
        imagem = Image.open(io.BytesIO(conteudo))
        if imagem.mode not in ("RGBA", "LA", "PA") and "transparency" not in imagem.info:
            return False  # formato sem canal alfa (JPEG, por exemplo)
        menor_alfa, _maior = imagem.convert("RGBA").getchannel("A").getextrema()
        return menor_alfa < 250
    except Exception:
        return False


# Especialidades médicas mais comuns pro <select> — se não estiver na lista,
# o profissional pode digitar a própria em "Outra especialidade".
ESPECIALIDADES_MEDICAS = [
    "Clínico geral",
    "Cardiologista",
    "Dermatologista",
    "Pediatra",
    "Ginecologista/Obstetra",
    "Ortopedista",
    "Psiquiatra",
    "Neurologista",
    "Oftalmologista",
    "Otorrinolaringologista",
    "Urologista",
    "Endocrinologista",
    "Gastroenterologista",
    "Reumatologista",
    "Anestesiologista",
    "Nefrologista",
    "Pneumologista",
    "Infectologista",
    "Geriatra",
]

# Ícone (emoji) de cada categoria, usado na fileira de categorias da home e
# nos cards do catálogo. Categoria nova/"Outro" que não estiver aqui cai no
# ícone genérico 🔧.
CATEGORIA_EMOJIS = {
    "Eletricista": "⚡",
    "Encanador": "🚿",
    "Vidraceiro": "🪟",
    "Pedreiro / Reformas": "🧱",
    "Pintor": "🎨",
    "Gesseiro": "🏗️",
    "Serralheiro": "🛠️",
    "Marceneiro": "🪚",
    "Chaveiro": "🔑",
    "Marido de aluguel": "🔧",
    "Telhadista / Impermeabilização": "🏚️",
    "Gás": "🔥",

    "Diarista / Faxina": "🧹",
    "Dedetização": "🐜",
    "Montador de móveis": "🪑",
    "Piscineiro": "🏊",
    "Jardinagem": "🌳",
    "Ar-condicionado / Refrigeração": "❄️",
    "Tapeceiro / Estofados": "🛋️",
    "Limpeza pós-obra": "🧽",

    "Mecânico/Oficina": "🔩",
    "Auto elétrico": "🔋",
    "Funilaria e Pintura": "🚙",
    "Guincho": "🚛",
    "Lavagem de carros": "🧼",

    "Informática & Tecnologia": "💻",
    "Conserto de celular": "📱",
    "Instalação de câmeras / CFTV": "🎥",
    "Assistência de eletrônicos": "🔌",

    "Médico": "🩺",
    "Dentista": "🦷",
    "Babá": "👶",
    "Cuidador de idosos": "🧓",
    "Personal trainer": "🏋️",
    "Fisioterapeuta": "🦵",
    "Psicólogo": "🧠",
    "Nutricionista": "🥗",

    "Cabeleireiro": "💇",
    "Manicure": "💅",
    "Podóloga": "🦶",
    "Barbeiro": "💈",
    "Salão de Beleza": "✂️",
    "Maquiador(a)": "💄",
    "Fotógrafo": "📸",
    "Confeiteiro(a) / Doceiro(a)": "🧁",
    "Buffet / Garçom": "🍽️",
    "DJ / Som": "🎵",
    "Decoração de festas": "🎈",

    "Aulas particulares": "📖",
    "Professor de idiomas": "🗣️",
    "Contador": "📊",
    "Advogado": "⚖️",

    "Frete / Mudança": "🚚",
    "Motoboy / Entregador": "🛵",
    "Motorista Particular / Uber": "🚕",

    # Categorias criadas pelo admin depois da lista original acima (via painel
    # /admin) -- sem ícone próprio elas caem no genérico 🔧 e destoam das
    # demais no menu/grade do catálogo, por isso ganham entrada aqui também.
    "Gás encanado": "🛢️",
    "Blogueiro(a)": "✍️",
    "Cesta de Café da Manhã": "🧺",
    "Serviços domésticoa": "🏡",  # nome como está cadastrado hoje (tem um erro de digitação -- ver painel admin)
    "Serviços Técnicos": "🧰",
    "Técnico em TV": "📺",
}
templates.env.globals["categoria_emoji"] = lambda nome: CATEGORIA_EMOJIS.get(nome, "🔧")

# Ícone de cada grupo amplo (usado no menu de categorias, estilo "mega menu").
GRUPO_EMOJIS = {
    "Casa e Reformas": "🏠",
    "Limpeza e Manutenção": "🧹",
    "Carros e Motos": "🚗",
    "Tecnologia": "💻",
    "Saúde e Família": "🩺",
    "Beleza e Autocuidado": "💅",
    "Eventos": "🎉",
    "Aulas e Consultoria": "📚",
    "Transporte": "🚚",
    "Outros": "🗂️",  # categoria criada sem grupo (ex: "Outro" no cadastro) cai aqui
}
templates.env.globals["grupo_emoji"] = lambda nome: GRUPO_EMOJIS.get(nome, "🔧")
templates.env.globals["categoria_tem_icone"] = lambda nome: nome in CATEGORIA_EMOJIS

# Categorias mostradas em destaque na home de quem não está logado —
# curadoria manual das mais buscadas, pra não sobrecarregar a tela
# com as ~50 categorias inteiras.
CATEGORIAS_DESTAQUE_LANDING = [
    "Eletricista",
    "Mecânico/Oficina",
    "Diarista / Faxina",
    "Manicure",
    "Encanador",
    "Informática & Tecnologia",
    "Pedreiro / Reformas",
    "Ar-condicionado / Refrigeração",
    "Conserto de celular",
    "Cabeleireiro",
]

# Atalho rápido de categorias no topo da busca (mobile e desktop): as 5 mais
# procuradas, com rótulo curto pra caber num chip pequeno. NENHUMA categoria
# é removida do site -- as ~50 continuam acessíveis pelo "Ver todas" e pelo
# menu por grupo. Cada item é (nome real da categoria, rótulo curto, emoji).
CATEGORIAS_POPULARES = [
    ("Eletricista", "Eletricista", "⚡"),
    ("Encanador", "Encanador", "🚿"),
    ("Mecânico/Oficina", "Mecânico", "🔧"),
    ("Diarista / Faxina", "Limpeza", "🧹"),
    ("Informática & Tecnologia", "Tecnologia", "💻"),
]


def categorias_populares(db: Session) -> list[dict]:
    """Devolve as categorias populares que realmente existem no banco, na
    ordem de CATEGORIAS_POPULARES. Se alguma não existir (banco novo, nome
    editado), ela simplesmente não aparece -- nada é inventado."""
    por_nome = {
        c.nome: c
        for c in db.query(models.Category)
        .filter(models.Category.nome.in_([n for n, _, _ in CATEGORIAS_POPULARES]))
        .all()
    }
    return [
        {"id": por_nome[nome].id, "rotulo": rotulo, "emoji": emoji}
        for nome, rotulo, emoji in CATEGORIAS_POPULARES
        if nome in por_nome
    ]


# Descrição curta de cada categoria em destaque, só pro card ficar mais
# informativo na home — puramente texto de apresentação, não afeta o filtro.
CATEGORIA_DESCRICOES_DESTAQUE = {
    "Eletricista": "Instalações e reparos",
    "Mecânico/Oficina": "Manutenção e oficina",
    "Diarista / Faxina": "Limpeza residencial",
    "Manicure": "Unhas e cuidados",
    "Encanador": "Vazamentos e hidráulica",
    "Informática & Tecnologia": "Computadores e redes",
    "Pedreiro / Reformas": "Obras e reformas",
    "Ar-condicionado / Refrigeração": "Instalação e manutenção",
    "Conserto de celular": "Telas e reparos",
    "Cabeleireiro": "Cortes e tratamentos",
}
templates.env.globals["categoria_descricao"] = lambda nome: CATEGORIA_DESCRICOES_DESTAQUE.get(nome, "")


# ---------------------------------------------------------------------------
# SEO: slugs amigáveis pra URL (ex: "Ar-condicionado / Refrigeração" -> "ar-condicionado-refrigeracao")
# ---------------------------------------------------------------------------

def _slugify(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    texto = texto.lower().strip()
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return texto


# ---------------------------------------------------------------------------
# Catálogo (página inicial)
# ---------------------------------------------------------------------------

ULTIMOS_PROFISSIONAIS_LIMITE = 12


@app.get("/api/ultimos-profissionais")
def api_ultimos_profissionais(db: Session = Depends(get_db)):
    """Últimos profissionais aprovados, pra faixa decorativa que passa
    devagar no fundo da home (ver landing.html) -- só dado público que já
    aparece no catálogo normalmente, nada sensível."""
    profissionais = (
        db.query(models.ProfessionalProfile)
        .filter(
            models.ProfessionalProfile.aprovado == True,  # noqa: E712
            models.ProfessionalProfile.ativo == True,  # noqa: E712
            models.ProfessionalProfile.usuario.has(models.User.tipo == "profissional"),
        )
        .order_by(models.ProfessionalProfile.criado_em.desc())
        .limit(ULTIMOS_PROFISSIONAIS_LIMITE)
        .all()
    )
    return [
        {
            "nome": p.usuario.nome,
            "cidade": p.cidade or "",
            "categoria": p.categorias[0].nome if p.categorias else "",
            "foto_url": p.foto_url or "",
        }
        for p in profissionais
    ]


def cidades_mais_ativas(db: Session, limite: int = 6) -> list[str]:
    """Cidades com mais profissionais cadastrados, pra oferecer como atalho
    de clique — pra quem não quer (ou não confia n)a localização automática
    do navegador (GPS de notebook/rede errando é comum, e o site não tem
    como controlar isso)."""
    linhas = (
        db.query(models.ProfessionalProfile.cidade)
        .filter(
            models.ProfessionalProfile.aprovado == True,  # noqa: E712
            models.ProfessionalProfile.ativo == True,  # noqa: E712
            models.ProfessionalProfile.usuario.has(models.User.tipo == "profissional"),
            models.ProfessionalProfile.cidade.isnot(None),
            models.ProfessionalProfile.cidade != "",
        )
        .group_by(models.ProfessionalProfile.cidade)
        .order_by(func.count(models.ProfessionalProfile.id).desc())
        .limit(limite)
        .all()
    )
    return [linha[0] for linha in linhas]


def _categorias_agrupadas(db: Session):
    """Categorias organizadas em grupos amplos, na ordem de GRUPO_EMOJIS
    (e alfabética dentro de cada grupo) -- mesma organização usada no mega
    menu da home, no checklist de categorias do profissional e no painel
    admin, pra tudo ficar consistente em vez de listas soltas e desalinhadas
    entre uma tela e outra. Categoria sem grupo definido (ex: criada via
    "Outro" no perfil) cai num grupo "Outros" no final."""
    categorias = db.query(models.Category).order_by(models.Category.nome).all()
    por_grupo = {}
    for c in categorias:
        por_grupo.setdefault(c.grupo or "Outros", []).append(c)
    ordem_grupos = list(GRUPO_EMOJIS.keys()) + [g for g in por_grupo if g not in GRUPO_EMOJIS]
    return [(g, por_grupo[g]) for g in ordem_grupos if g in por_grupo]


def _resultados_catalogo(
    request, db, usuario, *,
    categoria=None, grupo=None, estado=None, cidade=None, busca=None, ordenar="avaliacao",
    titulo_pagina=None, meta_descricao_pagina=None, h1_pagina=None,
):
    """Monta a resposta da tela de resultados (index.html) -- extraído do
    catalogo() pra ser reaproveitado pelas páginas de SEO por
    categoria+cidade (/servicos/...), que precisam da mesma listagem só
    que com título e descrição únicos pra cada combinação."""
    # O <select> de categoria manda "" quando é "Todas as categorias" —
    # não dá pra tipar o parâmetro como int direto, senão o FastAPI
    # rejeita a string vazia antes de chegar aqui.
    try:
        categoria_id = int(categoria) if categoria else None
    except ValueError:
        categoria_id = None

    # Aparecer no catálogo depende de três coisas, nessa ordem: a conta ser
    # do tipo "profissional", o perfil estar aprovado pelo admin e não estar
    # pausado. Uma conta que virou cliente sai daqui na hora, mesmo que o
    # perfil antigo continue guardado no banco.
    query = db.query(models.ProfessionalProfile).filter(
        models.ProfessionalProfile.aprovado == True,  # noqa: E712
        models.ProfessionalProfile.ativo == True,  # noqa: E712
        models.ProfessionalProfile.usuario.has(models.User.tipo == "profissional"),
    )

    if categoria_id:
        query = query.filter(models.ProfessionalProfile.categorias.any(models.Category.id == categoria_id))
    elif grupo:
        query = query.filter(models.ProfessionalProfile.categorias.any(models.Category.grupo == grupo))

    if estado:
        query = query.filter(models.ProfessionalProfile.estado == estado.strip().upper())

    if cidade:
        # O campo aceita cidade OU bairro (o rótulo já diz "Cidade / bairro"),
        # e é o mesmo campo usado pelo check-in por geolocalização.
        query = query.filter(or_(
            models.ProfessionalProfile.cidade.ilike(f"%{cidade}%"),
            models.ProfessionalProfile.bairro.ilike(f"%{cidade}%"),
        ))

    if busca:
        query = query.join(models.User).filter(
            or_(
                models.User.nome.ilike(f"%{busca}%"),
                models.ProfessionalProfile.descricao.ilike(f"%{busca}%"),
            )
        )

    profissionais = query.all()
    if ordenar == "recentes":
        profissionais.sort(key=lambda p: p.criado_em, reverse=True)
    else:
        ordenar = "avaliacao"
        profissionais.sort(key=lambda p: (p.nota_media, p.total_avaliacoes), reverse=True)

    # Sem nenhum filtro (busca "em branco"): mostra todo mundo, mas
    # agrupado por CIDADE (não bairro) -- o catálogo já tem gente de
    # municípios diferentes, e é o mesmo nível geográfico usado em todo
    # o resto do site (check-in, atalhos de cidade, páginas de SEO). Bairro
    # continua sendo um campo próprio no cadastro, só não é mais o critério
    # de agrupamento da home, com a cidade do usuário logado aparecendo
    # primeiro — assim é mais fácil achar quem atende perto de você.
    profissionais_por_cidade = None
    cidade_usuario = ""
    if not categoria_id and not grupo and not estado and not cidade and not busca:
        grupos = {}
        for p in profissionais:
            chave = (p.cidade or "").strip() or "Cidade não informada"
            grupos.setdefault(chave, []).append(p)

        if usuario:
            if usuario.cidade:
                cidade_usuario = usuario.cidade.strip()
            elif usuario.tipo == "profissional" and usuario.perfil_profissional and usuario.perfil_profissional.cidade:
                # Profissional já tem cidade cadastrada no perfil de atendimento —
                # não faz sentido pedir de novo em "Minha localização".
                cidade_usuario = usuario.perfil_profissional.cidade.strip()

        def ordem_cidade(nome):
            return (nome != cidade_usuario, nome == "Cidade não informada", nome.lower())

        profissionais_por_cidade = [(nome, grupos[nome]) for nome in sorted(grupos, key=ordem_cidade)]

    categorias = db.query(models.Category).order_by(models.Category.nome).all()
    categorias_por_grupo = _categorias_agrupadas(db)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "usuario": usuario,
            "profissionais": profissionais,
            "profissionais_por_cidade": profissionais_por_cidade,
            "cidade_usuario": cidade_usuario,
            "categorias": categorias,
            "categorias_por_grupo": categorias_por_grupo,
            "filtro_categoria": categoria_id,
            "filtro_grupo": grupo or "",
            "filtro_estado": estado or "",
            "filtro_cidade": cidade or "",
            "filtro_busca": busca or "",
            "ordenar": ordenar,
            "cidades_destaque": cidades_mais_ativas(db),
            "categorias_populares": categorias_populares(db),
            "eh_admin_usuario": eh_admin(usuario),
            "eh_dono_usuario": eh_dono(usuario),
            "titulo_pagina": titulo_pagina,
            "meta_descricao_pagina": meta_descricao_pagina,
            "h1_pagina": h1_pagina,
        },
    )


@app.get("/")
def catalogo(
    request: Request,
    categoria: str | None = None,
    grupo: str | None = None,
    estado: str | None = None,
    cidade: str | None = None,
    busca: str | None = None,
    explorar: str | None = None,
    ordenar: str = "avaliacao",
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    # Buscar e navegar o catálogo não exige conta — só pedimos login na hora
    # de agir de verdade (chamar no WhatsApp, ligar ou avaliar). Um visitante
    # sem nenhum filtro ainda cai na home de apresentação; qualquer busca,
    # filtro ou clique em "ver todas as categorias" (explorar) já mostra
    # os resultados de verdade.
    tem_filtro = bool(categoria or grupo or estado or cidade or busca or explorar)

    if not usuario and not tem_filtro:
        categorias_destaque = (
            db.query(models.Category)
            .filter(models.Category.nome.in_(CATEGORIAS_DESTAQUE_LANDING))
            .all()
        )
        categorias_destaque.sort(
            key=lambda c: CATEGORIAS_DESTAQUE_LANDING.index(c.nome)
            if c.nome in CATEGORIAS_DESTAQUE_LANDING
            else len(CATEGORIAS_DESTAQUE_LANDING)
        )

        # "Profissionais em destaque": só dados reais, nunca inventados. Pega
        # os aprovados/ativos, prioriza melhor avaliados e desempata pelos
        # mais recentes — assim quem acabou de ser aprovado também aparece,
        # não só quem já tem avaliação.
        candidatos = (
            db.query(models.ProfessionalProfile)
            .filter(
                models.ProfessionalProfile.aprovado == True,  # noqa: E712
                models.ProfessionalProfile.ativo == True,  # noqa: E712
                models.ProfessionalProfile.usuario.has(models.User.tipo == "profissional"),
            )
            .all()
        )
        candidatos.sort(key=lambda p: (p.nota_media, p.total_avaliacoes, p.criado_em), reverse=True)
        profissionais_destaque = candidatos[:4]

        # Foto real pro hero: o melhor avaliado que tenha foto cadastrada de
        # verdade (procura em todo mundo aprovado, não só no top 4) — se
        # ninguém tiver foto ainda, o hero simplesmente não mostra essa
        # coluna, em vez de usar uma imagem de banco de imagens.
        profissional_hero = next((p for p in candidatos if p.foto_url), None)

        nomes_categorias = [c.nome for c in db.query(models.Category).order_by(models.Category.nome).all()]

        return templates.TemplateResponse(
            "landing.html",
            {
                "request": request,
                "categorias_destaque": categorias_destaque,
                "categorias_populares": categorias_populares(db),
                "cidades_destaque": cidades_mais_ativas(db),
                "profissionais_destaque": profissionais_destaque,
                "profissional_hero": profissional_hero,
                "nomes_categorias": nomes_categorias,
            },
        )

    return _resultados_catalogo(
        request, db, usuario,
        categoria=categoria, grupo=grupo, estado=estado, cidade=cidade, busca=busca, ordenar=ordenar,
    )


# ---------------------------------------------------------------------------
# Páginas de SEO local: /servicos/<categoria>/<cidade>, ex: /servicos/eletricista/macae
# Mesma listagem do catálogo, só que com título/descrição únicos pra cada
# combinação -- é o formato de URL que melhor ranqueia pra busca tipo
# "eletricista em macaé".
# ---------------------------------------------------------------------------

@app.get("/servicos/{categoria_slug}/{cidade_slug}")
def pagina_seo_categoria_cidade(
    categoria_slug: str,
    cidade_slug: str,
    request: Request,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    categoria = next(
        (c for c in db.query(models.Category).all() if _slugify(c.nome) == categoria_slug),
        None,
    )
    if not categoria:
        return RedirectResponse("/?explorar=1", status_code=303)

    # A cidade é texto livre digitado por cada profissional -- não dá pra
    # ter uma lista fixa de slugs válidos, então batemos o slug da URL
    # contra as cidades que realmente existem no catálogo. Se não achar
    # nenhuma (ex: alguém digitou uma cidade errada na URL), mostra a
    # categoria em todas as cidades em vez de dar uma página quebrada.
    cidades_reais = (
        db.query(models.ProfessionalProfile.cidade)
        .filter(models.ProfessionalProfile.cidade.isnot(None), models.ProfessionalProfile.cidade != "")
        .distinct()
        .all()
    )
    cidade_real = next((c[0] for c in cidades_reais if _slugify(c[0]) == cidade_slug), None)

    if cidade_real:
        titulo = f"{categoria.nome} em {cidade_real} — SocorreAqui"
        h1 = f"{categoria.nome} em {cidade_real}"
        meta = f"Encontre {categoria.nome.lower()} em {cidade_real}. Veja avaliações e chame direto no WhatsApp, sem intermediário."
    else:
        titulo = f"{categoria.nome} — SocorreAqui"
        h1 = categoria.nome
        meta = f"Encontre {categoria.nome.lower()} perto de você. Veja avaliações e chame direto no WhatsApp, sem intermediário."

    return _resultados_catalogo(
        request, db, usuario,
        categoria=str(categoria.id), cidade=cidade_real,
        titulo_pagina=titulo, meta_descricao_pagina=meta, h1_pagina=h1,
    )


SITE_URL = "https://socorreaqui.tec.br"


@app.get("/googlefcb4228506c22a8f.html")
def google_search_console_verificacao():
    # Arquivo de verificação de propriedade do Google Search Console --
    # o conteúdo precisa ser exatamente esse (o nome do arquivo repetido
    # como texto), e o arquivo nunca pode ser removido, senão a
    # verificação é perdida.
    return PlainTextResponse("google-site-verification: googlefcb4228506c22a8f.html")


@app.get("/robots.txt")
def robots_txt():
    conteudo = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /minha-localizacao\n"
        "Disallow: /profissional/perfil/editar\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )
    return PlainTextResponse(conteudo)


@app.get("/sitemap.xml")
def sitemap_xml(db: Session = Depends(get_db)):
    """Só lista páginas com conteúdo de verdade (sem thin content): a home,
    combinações categoria+cidade que têm pelo menos um profissional
    aprovado, e o perfil público de cada um deles."""
    urls = [(f"{SITE_URL}/", "1.0")]

    aprovados = (
        db.query(models.ProfessionalProfile)
        .filter(
            models.ProfessionalProfile.aprovado == True,  # noqa: E712
            models.ProfessionalProfile.ativo == True,  # noqa: E712
            models.ProfessionalProfile.usuario.has(models.User.tipo == "profissional"),
        )
        .all()
    )
    for p in aprovados:
        urls.append((f"{SITE_URL}/profissional/{p.id}", "0.7"))

    combinacoes = set()
    for p in aprovados:
        if not p.cidade:
            continue
        for cat in p.categorias:
            combinacoes.add((_slugify(cat.nome), _slugify(p.cidade)))
    for categoria_slug, cidade_slug in sorted(combinacoes):
        if categoria_slug and cidade_slug:
            urls.append((f"{SITE_URL}/servicos/{categoria_slug}/{cidade_slug}", "0.8"))

    itens_xml = "".join(
        f"<url><loc>{loc}</loc><priority>{prioridade}</priority></url>"
        for loc, prioridade in urls
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{itens_xml}"
        "</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Cadastro / Login / Logout
# ---------------------------------------------------------------------------

def _next_seguro(next: str | None) -> str:
    """Só aceita caminhos internos (começando com uma única "/") como destino
    pós-login — evita que alguém use ?next= pra redirecionar a vítima de um
    link malicioso pra fora do site (open redirect)."""
    if next and next.startswith("/") and not next.startswith("//") and not next.startswith("/\\"):
        return next
    return "/"


def _msg(texto: str) -> str:
    """URL-encode pra embutir uma mensagem de aviso em ?mensagem=... num
    redirect (ex: pós-confirmação de e-mail, pós-redefinição de senha)."""
    from urllib.parse import quote
    return quote(texto)


def _url_base(request: Request) -> str:
    """URL base pra montar link clicável de e-mail (confirmação, redefinição
    de senha). Usa o domínio de produção quando publicado -- em dev, o
    próprio host da requisição (senão o link do e-mail apontaria sempre pro
    site de verdade, mesmo testando local)."""
    return SITE_URL if RODANDO_EM_PRODUCAO else str(request.base_url).rstrip("/")


TOKEN_MAX_IDADE_CONFIRMACAO = 60 * 60 * 24 * 3  # 3 dias
TOKEN_MAX_IDADE_RESET_SENHA = 60 * 60  # 1 hora


def _enviar_confirmacao_email(request: Request, usuario: "models.User") -> None:
    token = auth.gerar_token(SECRET_KEY, "confirmar-email", {"uid": usuario.id})
    link = f"{_url_base(request)}/confirmar-email?token={token}"
    email_utils.enviar_email_confirmacao(usuario.email, usuario.nome, link)


@app.get("/comecar")
def form_comecar(request: Request, tipo: str = "cliente", next: str | None = None):
    return templates.TemplateResponse(
        "comecar.html",
        {
            "request": request, "tipo": tipo if tipo in ("cliente", "profissional") else "cliente",
            "next": _next_seguro(next) if next else "",
        },
    )


@app.get("/cadastro")
def form_cadastro(request: Request, tipo: str = "cliente", next: str | None = None):
    return templates.TemplateResponse(
        "cadastro.html",
        {
            "request": request, "erro": None, "google_habilitado": google_oauth_habilitado,
            "tipo_selecionado": tipo if tipo in ("cliente", "profissional") else "cliente",
            "next": _next_seguro(next) if next else "",
        },
    )


@app.post("/cadastro")
def cadastrar(
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
    telefone: str = Form(...),
    senha: str = Form(...),
    tipo: str = Form(...),
    cidade: str = Form(""),
    bairro: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    nome = nome.strip()
    email = email.strip().lower()
    telefone = telefone.strip()

    def erro(mensagem):
        return templates.TemplateResponse(
            "cadastro.html",
            {
                "request": request, "erro": mensagem, "google_habilitado": google_oauth_habilitado,
                "tipo_selecionado": tipo if tipo in ("cliente", "profissional") else "cliente",
                "next": next,
            },
        )

    # Limite por IP -- sem isso, um bot consegue criar conta atrás de conta
    # (spam no catálogo, flood do e-mail que avisa o admin de novo cadastro).
    chave_limite = _ip_cliente(request)
    if _acao_bloqueada("cadastro", chave_limite, CADASTRO_MAX_TENTATIVAS, CADASTRO_JANELA_SEGUNDOS):
        return erro("Muitas tentativas de cadastro. Aguarde um pouco e tente de novo.")
    _registrar_tentativa("cadastro", chave_limite)

    if not EMAIL_REGEX.match(email):
        return erro("Digite um e-mail válido.")
    # Mensagem genérica de propósito -- não diz se foi o e-mail ou o telefone
    # que já existe, pra não dar pra alguém checar "esse e-mail já tem
    # conta?" só tentando cadastrar (enumeração de contas).
    ja_existe = (
        db.query(models.User).filter(models.User.email == email).first()
        or db.query(models.User).filter(models.User.telefone == telefone).first()
    )
    if ja_existe:
        return erro("Já existe uma conta com esse e-mail ou telefone. Se for sua, faça login.")

    novo_usuario = models.User(
        nome=nome,
        email=email,
        telefone=telefone,
        senha_hash=auth.gerar_hash_senha(senha),
        tipo=tipo,
        cidade=cidade.strip() or None,
        bairro=bairro.strip() or None,
        email_verificado=False,
    )
    db.add(novo_usuario)
    db.commit()
    db.refresh(novo_usuario)

    if tipo == "profissional":
        perfil = models.ProfessionalProfile(usuario_id=novo_usuario.id, cidade="")
        db.add(perfil)
        db.commit()

    _avisar_admin_novo_cadastro(novo_usuario)
    _enviar_confirmacao_email(request, novo_usuario)
    # Deixa a pessoa navegar logo após o cadastro (não trava o onboarding
    # esperando o clique no e-mail) -- a confirmação só é cobrada da próxima
    # vez que ela precisar logar de novo, em login().
    request.session["user_id"] = novo_usuario.id

    if tipo == "profissional":
        return RedirectResponse("/profissional/perfil/editar", status_code=303)
    return RedirectResponse(_next_seguro(next), status_code=303)


@app.get("/login")
def form_login(request: Request, next: str | None = None, mensagem: str | None = None):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request, "erro": None, "mensagem": mensagem, "google_habilitado": google_oauth_habilitado,
            "next": _next_seguro(next) if next else "",
        },
    )


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()

    def erro(mensagem, **extra):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request, "erro": mensagem, "mensagem": None,
                "google_habilitado": google_oauth_habilitado, "next": next, **extra,
            },
        )

    chave_limite = _ip_cliente(request)
    if _login_bloqueado(chave_limite):
        return erro("Muitas tentativas de login. Aguarde alguns minutos e tente de novo.")

    usuario = db.query(models.User).filter(models.User.email == email).first()

    # Mensagem sempre igual (não diz se o e-mail existe, se a senha é
    # errada, ou se a conta é só de Google) -- evita dar pra alguém
    # descobrir quais e-mails têm conta só de tentar logar com eles. Quem
    # cadastrou com Google já vê o botão "Entrar com Google" na mesma tela.
    if (
        not usuario
        or not usuario.senha_hash
        or not auth.verificar_senha(senha, usuario.senha_hash)
    ):
        _registrar_tentativa_falha(chave_limite)
        return erro("E-mail ou senha incorretos.")

    if not usuario.ativo:
        return erro("Essa conta foi bloqueada. Entre em contato com o suporte.")

    if not usuario.email_verificado:
        return erro(
            "Confirme seu e-mail antes de entrar. Verifique sua caixa de entrada (e o spam).",
            email_nao_confirmado=email,
        )

    _limpar_tentativas("login", chave_limite)
    usuario.ultimo_login_em = datetime.utcnow()
    db.commit()
    request.session["user_id"] = usuario.id
    return RedirectResponse(_next_seguro(next), status_code=303)


# ---------------------------------------------------------------------------
# Confirmação de e-mail
# ---------------------------------------------------------------------------

@app.get("/confirmar-email")
def confirmar_email(token: str, db: Session = Depends(get_db)):
    dados = auth.ler_token(SECRET_KEY, "confirmar-email", token, TOKEN_MAX_IDADE_CONFIRMACAO)
    if not dados:
        return RedirectResponse(
            "/login?mensagem=" + _msg("Link de confirmação inválido ou vencido. Peça um novo abaixo."),
            status_code=303,
        )

    usuario = db.query(models.User).filter(models.User.id == dados.get("uid")).first()
    if not usuario:
        return RedirectResponse("/login", status_code=303)

    if not usuario.email_verificado:
        usuario.email_verificado = True
        db.commit()

    return RedirectResponse(
        "/login?mensagem=" + _msg("E-mail confirmado! Agora é só entrar."), status_code=303,
    )


@app.get("/reenviar-confirmacao")
def form_reenviar_confirmacao(request: Request, email: str = ""):
    return templates.TemplateResponse(
        "reenviar_confirmacao.html", {"request": request, "email": email, "enviado": False},
    )


@app.post("/reenviar-confirmacao")
def reenviar_confirmacao(
    request: Request, email: str = Form(...), db: Session = Depends(get_db),
):
    email = email.strip().lower()
    chave_limite = _ip_cliente(request)
    # Resposta é sempre "enviado" pra tela, mesmo sem mandar e-mail de
    # verdade (limite estourado, e-mail não cadastrado, ou já confirmado) --
    # não dá pra deixar alguém descobrir por aqui quais e-mails têm conta.
    if not _acao_bloqueada(
        "reenvio-confirmacao", chave_limite,
        REENVIO_CONFIRMACAO_MAX_TENTATIVAS, REENVIO_CONFIRMACAO_JANELA_SEGUNDOS,
    ):
        _registrar_tentativa("reenvio-confirmacao", chave_limite)
        usuario = db.query(models.User).filter(models.User.email == email).first()
        if usuario and not usuario.email_verificado:
            _enviar_confirmacao_email(request, usuario)

    return templates.TemplateResponse(
        "reenviar_confirmacao.html", {"request": request, "email": email, "enviado": True},
    )


# ---------------------------------------------------------------------------
# Esqueci minha senha
# ---------------------------------------------------------------------------

@app.get("/esqueci-senha")
def form_esqueci_senha(request: Request):
    return templates.TemplateResponse(
        "esqueci_senha.html", {"request": request, "enviado": False, "erro": None},
    )


@app.post("/esqueci-senha")
def esqueci_senha(
    request: Request, email: str = Form(...), db: Session = Depends(get_db),
):
    email = email.strip().lower()
    chave_limite = _ip_cliente(request)

    # Mesma resposta genérica pra qualquer caso (e-mail existe ou não, é só
    # de Google ou não, limite estourado ou não) -- evita enumeração de
    # contas e evita que alguém veja diferença de comportamento.
    if not _acao_bloqueada(
        "esqueci-senha", chave_limite, RESET_SENHA_MAX_TENTATIVAS, RESET_SENHA_JANELA_SEGUNDOS,
    ):
        _registrar_tentativa("esqueci-senha", chave_limite)
        usuario = db.query(models.User).filter(models.User.email == email).first()
        if usuario and usuario.senha_hash:
            # Assina o hash atual da senha junto -- assim, se a pessoa já
            # trocou a senha (ou pediu outro link depois), o link antigo
            # para de funcionar sozinho, sem precisar guardar token no banco.
            token = auth.gerar_token(
                SECRET_KEY, "redefinir-senha",
                {"uid": usuario.id, "h": usuario.senha_hash[-16:]},
            )
            link = f"{_url_base(request)}/redefinir-senha?token={token}"
            email_utils.enviar_email_redefinicao_senha(usuario.email, usuario.nome, link)

    return templates.TemplateResponse(
        "esqueci_senha.html", {"request": request, "enviado": True, "erro": None},
    )


@app.get("/redefinir-senha")
def form_redefinir_senha(request: Request, token: str):
    dados = auth.ler_token(SECRET_KEY, "redefinir-senha", token, TOKEN_MAX_IDADE_RESET_SENHA)
    if not dados:
        return templates.TemplateResponse(
            "redefinir_senha.html",
            {"request": request, "token": None, "erro": "Link inválido ou vencido. Peça um novo."},
        )
    return templates.TemplateResponse(
        "redefinir_senha.html", {"request": request, "token": token, "erro": None},
    )


@app.post("/redefinir-senha")
def redefinir_senha(
    request: Request,
    token: str = Form(...),
    senha: str = Form(...),
    senha_confirmar: str = Form(...),
    db: Session = Depends(get_db),
):
    def erro(mensagem, token_valido=True):
        return templates.TemplateResponse(
            "redefinir_senha.html",
            {"request": request, "token": token if token_valido else None, "erro": mensagem},
        )

    dados = auth.ler_token(SECRET_KEY, "redefinir-senha", token, TOKEN_MAX_IDADE_RESET_SENHA)
    if not dados:
        return erro("Link inválido ou vencido. Peça um novo.", token_valido=False)

    usuario = db.query(models.User).filter(models.User.id == dados.get("uid")).first()
    if not usuario or not usuario.senha_hash or usuario.senha_hash[-16:] != dados.get("h"):
        return erro("Link inválido ou vencido. Peça um novo.", token_valido=False)

    if len(senha) < 6:
        return erro("A senha precisa ter pelo menos 6 caracteres.")
    if senha != senha_confirmar:
        return erro("As senhas não são iguais.")

    usuario.senha_hash = auth.gerar_hash_senha(senha)
    db.commit()

    return RedirectResponse(
        "/login?mensagem=" + _msg("Senha redefinida! Agora é só entrar com a senha nova."),
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Login com Google
# ---------------------------------------------------------------------------

@app.get("/auth/google/login")
async def google_login(request: Request, next: str | None = None):
    if not google_oauth_habilitado:
        return RedirectResponse("/login", status_code=303)
    # Guarda o destino na sessão -- a ida e volta pro Google não preserva
    # query string nenhuma além da que o próprio OAuth usa.
    request.session["oauth_next"] = _next_seguro(next) if next else ""
    redirect_uri = request.url_for("google_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    if not google_oauth_habilitado:
        return RedirectResponse("/login", status_code=303)

    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or await oauth.google.userinfo(token=token)
    email = userinfo["email"].strip().lower()
    nome = userinfo.get("name") or email.split("@")[0]
    google_id = userinfo["sub"]

    proximo = request.session.pop("oauth_next", "") or "/"
    usuario = db.query(models.User).filter(models.User.email == email).first()

    if usuario:
        if not usuario.ativo:
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request, "google_habilitado": google_oauth_habilitado,
                    "erro": "Essa conta foi bloqueada. Entre em contato com o suporte.",
                },
            )
        if not usuario.google_id:
            usuario.google_id = google_id
        if not usuario.email_verificado:
            usuario.email_verificado = True
        usuario.ultimo_login_em = datetime.utcnow()
        db.commit()
        request.session["user_id"] = usuario.id
        return RedirectResponse(proximo, status_code=303)

    # Conta nova via Google: ainda falta telefone (contato) e tipo de conta.
    # Guarda o destino de novo, porque completar-cadastro-google é mais um
    # redirect antes do login de verdade acontecer.
    request.session["google_pendente"] = {"email": email, "nome": nome, "google_id": google_id}
    request.session["oauth_next"] = proximo
    return RedirectResponse("/completar-cadastro-google", status_code=303)


@app.get("/completar-cadastro-google")
def form_completar_cadastro_google(request: Request):
    pendente = request.session.get("google_pendente")
    if not pendente:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "completar_cadastro_google.html",
        {"request": request, "nome": pendente["nome"], "email": pendente["email"], "erro": None},
    )


@app.post("/completar-cadastro-google")
def completar_cadastro_google(
    request: Request,
    telefone: str = Form(...),
    tipo: str = Form(...),
    cidade: str = Form(""),
    bairro: str = Form(""),
    db: Session = Depends(get_db),
):
    pendente = request.session.get("google_pendente")
    if not pendente:
        return RedirectResponse("/login", status_code=303)

    telefone = telefone.strip()

    if db.query(models.User).filter(models.User.telefone == telefone).first():
        return templates.TemplateResponse(
            "completar_cadastro_google.html",
            {
                "request": request, "nome": pendente["nome"], "email": pendente["email"],
                "erro": "Já existe um cadastro com esse telefone.",
            },
        )

    novo_usuario = models.User(
        nome=pendente["nome"],
        email=pendente["email"],
        telefone=telefone,
        senha_hash=None,
        google_id=pendente["google_id"],
        tipo=tipo,
        cidade=cidade.strip() or None,
        bairro=bairro.strip() or None,
        email_verificado=True,  # o Google já confirmou esse e-mail
    )
    db.add(novo_usuario)
    db.commit()
    db.refresh(novo_usuario)

    if tipo == "profissional":
        perfil = models.ProfessionalProfile(usuario_id=novo_usuario.id, cidade="")
        db.add(perfil)
        db.commit()

    _avisar_admin_novo_cadastro(novo_usuario)
    del request.session["google_pendente"]
    proximo = request.session.pop("oauth_next", "") or "/"
    request.session["user_id"] = novo_usuario.id

    if tipo == "profissional":
        return RedirectResponse("/profissional/perfil/editar", status_code=303)
    return RedirectResponse(proximo, status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Meus dados (nome, cidade/bairro do usuário — usado pra agrupar o catálogo)
# ---------------------------------------------------------------------------

@app.get("/minha-localizacao")
def form_minha_localizacao(request: Request, usuario=Depends(auth.usuario_logado)):
    if not usuario:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "minha_localizacao.html", {"request": request, "usuario": usuario, "erro": None},
    )


@app.post("/minha-localizacao")
def salvar_minha_localizacao(
    request: Request,
    nome: str = Form(""),
    cidade: str = Form(""),
    bairro: str = Form(""),
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not usuario:
        return RedirectResponse("/login", status_code=303)

    nome = nome.strip()
    if not nome:
        return templates.TemplateResponse(
            "minha_localizacao.html",
            {"request": request, "usuario": usuario, "erro": "O nome não pode ficar em branco."},
        )

    usuario.nome = nome
    usuario.cidade = cidade.strip() or None
    usuario.bairro = bairro.strip() or None
    db.commit()

    return RedirectResponse("/", status_code=303)


@app.post("/minha-conta/excluir")
def excluir_minha_conta(
    request: Request,
    senha: str = Form(""),
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not usuario:
        return RedirectResponse("/login", status_code=303)

    def erro(mensagem):
        return templates.TemplateResponse(
            "minha_localizacao.html", {"request": request, "usuario": usuario, "erro": mensagem},
        )

    if eh_admin(usuario):
        return erro("A conta admin não pode se autoexcluir.")

    # Quem tem senha (cadastro normal, não só Google) precisa confirmar ela
    # antes de apagar a conta -- evita apagar sem querer por um clique
    # errado, e evita que um CSRF vindo de outro site consiga apagar a
    # conta sem a pessoa digitar a senha de novo.
    if usuario.senha_hash and not auth.verificar_senha(senha, usuario.senha_hash):
        return erro("Senha incorreta -- a conta não foi excluída.")

    db.delete(usuario)
    db.commit()
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Perfil público do profissional + avaliação
# ---------------------------------------------------------------------------

@app.get("/profissional/{profissional_id}")
def ver_profissional(
    profissional_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    # O perfil público pode ser visto sem login — só avaliar e chamar no
    # WhatsApp exigem conta (a própria tela já sabia lidar com usuario=None
    # no bloco de avaliação; só faltava permitir chegar até aqui sem login).
    perfil = db.query(models.ProfessionalProfile).filter(
        models.ProfessionalProfile.id == profissional_id
    ).first()
    if not perfil:
        return RedirectResponse("/", status_code=303)
    # Conta que não é mais do tipo "profissional" não tem perfil público --
    # o admin continua enxergando tudo pelo painel.
    if perfil.usuario.tipo != "profissional" and not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)

    avaliacoes = sorted(perfil.avaliacoes, key=lambda r: r.criado_em, reverse=True)

    # Qualquer pessoa logada pode avaliar, contanto que não seja o próprio
    # dono do perfil se autoavaliando (cliente ou profissional avaliando
    # outro profissional são ambos permitidos).
    pode_avaliar = bool(usuario) and usuario.id != perfil.usuario_id
    minha_avaliacao = None
    if usuario and pode_avaliar:
        minha_avaliacao = next((r for r in avaliacoes if r.cliente_id == usuario.id), None)

    return templates.TemplateResponse(
        "perfil_profissional.html",
        {
            "request": request,
            "usuario": usuario,
            "perfil": perfil,
            "avaliacoes": avaliacoes,
            "minha_avaliacao": minha_avaliacao,
            "pode_avaliar": pode_avaliar,
            "eh_admin_usuario": eh_admin(usuario),
            "eh_dono_usuario": eh_dono(usuario),
        },
    )


@app.post("/profissional/{profissional_id}/avaliar")
def avaliar(
    profissional_id: int,
    request: Request,
    estrelas: int = Form(...),
    comentario: str = Form(""),
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not usuario:
        return RedirectResponse("/login", status_code=303)

    perfil = db.query(models.ProfessionalProfile).filter(
        models.ProfessionalProfile.id == profissional_id
    ).first()
    # Ninguém pode se autoavaliar — vale tanto pra cliente quanto pra
    # profissional avaliando outro profissional (a tela já esconde o
    # formulário nesse caso, mas a checagem tem que valer aqui também).
    if not perfil or usuario.id == perfil.usuario_id:
        return RedirectResponse(f"/profissional/{profissional_id}", status_code=303)

    estrelas = max(1, min(5, estrelas))  # trava entre 1 e 5

    # Um cliente só tem uma avaliação por profissional: se ele avaliar de
    # novo, atualiza a que já existia em vez de criar outra (evita inflar
    # a nota média com avaliações repetidas da mesma pessoa).
    review = db.query(models.Review).filter(
        models.Review.profissional_id == profissional_id,
        models.Review.cliente_id == usuario.id,
    ).first()
    if review:
        review.estrelas = estrelas
        review.comentario = comentario.strip() or None
    else:
        review = models.Review(
            profissional_id=profissional_id,
            cliente_id=usuario.id,
            estrelas=estrelas,
            comentario=comentario.strip() or None,
        )
        db.add(review)
    db.commit()

    return RedirectResponse(f"/profissional/{profissional_id}", status_code=303)


@app.post("/profissional/{profissional_id}/avaliar/excluir")
def excluir_avaliacao(
    profissional_id: int,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not usuario or usuario.tipo != "cliente":
        return RedirectResponse("/login", status_code=303)

    db.query(models.Review).filter(
        models.Review.profissional_id == profissional_id,
        models.Review.cliente_id == usuario.id,
    ).delete()
    db.commit()

    return RedirectResponse(f"/profissional/{profissional_id}", status_code=303)


# ---------------------------------------------------------------------------
# Indicação de profissional (usuário logado indica alguém que ainda não
# está cadastrado no app; o admin vê e entra em contato)
# ---------------------------------------------------------------------------

@app.get("/indicar")
def form_indicar(
    request: Request,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not usuario:
        return RedirectResponse("/login", status_code=303)

    return templates.TemplateResponse(
        "indicar.html",
        {
            "request": request, "usuario": usuario,
            "categorias_por_grupo": _categorias_agrupadas(db), "enviado": False,
        },
    )


@app.post("/indicar")
def indicar(
    request: Request,
    nome_profissional: str = Form(...),
    telefone_profissional: str = Form(...),
    categoria_id: str = Form(""),
    estado: str = Form(""),
    cidade: str = Form(""),
    observacao: str = Form(""),
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not usuario:
        return RedirectResponse("/login", status_code=303)

    nome_profissional = nome_profissional.strip()
    telefone_profissional = telefone_profissional.strip()
    estado = estado.strip().upper()
    cidade = cidade.strip()

    try:
        categoria_id_int = int(categoria_id) if categoria_id else None
    except ValueError:
        categoria_id_int = None

    def erro(mensagem):
        return templates.TemplateResponse(
            "indicar.html",
            {
                "request": request, "usuario": usuario,
                "categorias_por_grupo": _categorias_agrupadas(db), "enviado": False,
                "erro": mensagem,
                "valores": {
                    "nome_profissional": nome_profissional,
                    "telefone_profissional": telefone_profissional,
                    "categoria_id": categoria_id,
                    "estado": estado,
                    "cidade": cidade,
                },
            },
        )

    # Nome/telefone já são obrigatórios no HTML, mas a categoria e a cidade
    # também precisam vir preenchidas — sem isso não dá pra saber o que
    # oferecer nem onde a pessoa atende na hora de convidar ela pra plataforma.
    if not nome_profissional or not telefone_profissional:
        return erro("Preencha o nome e o telefone do profissional.")
    if not categoria_id_int:
        return erro("Selecione a categoria do profissional.")
    if not estado:
        return erro("Selecione o estado do profissional.")
    if not cidade:
        return erro("Informe a cidade do profissional.")

    indicacao = models.Indicacao(
        indicado_por_id=usuario.id,
        nome_profissional=nome_profissional,
        telefone_profissional=telefone_profissional,
        categoria_id=categoria_id_int,
        estado=estado,
        cidade=cidade,
        observacao=observacao.strip() or None,
    )
    db.add(indicacao)
    db.commit()

    return templates.TemplateResponse(
        "indicar.html",
        {"request": request, "usuario": usuario, "enviado": True},
    )


# ---------------------------------------------------------------------------
# Área do profissional (editar o próprio perfil)
# ---------------------------------------------------------------------------

@app.get("/profissional/perfil/editar")
def form_editar_perfil(
    request: Request,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not usuario or usuario.tipo != "profissional":
        return RedirectResponse("/login", status_code=303)

    perfil = usuario.perfil_profissional
    categorias_selecionadas = {c.id for c in perfil.categorias} if perfil else set()

    return templates.TemplateResponse(
        "editar_perfil.html",
        {
            "request": request,
            "usuario": usuario,
            "perfil": perfil,
            "categorias_por_grupo": _categorias_agrupadas(db),
            "categorias_selecionadas": categorias_selecionadas,
            "especialidades_medicas": ESPECIALIDADES_MEDICAS,
            "erro": None,
        },
    )


def _erro_editar_perfil(request, db, usuario, perfil, mensagem):
    """Reexibe o formulário de editar perfil com uma mensagem de erro."""
    categorias_selecionadas = {c.id for c in perfil.categorias} if perfil else set()
    return templates.TemplateResponse(
        "editar_perfil.html",
        {
            "request": request,
            "usuario": usuario,
            "perfil": perfil,
            "categorias_por_grupo": _categorias_agrupadas(db),
            "categorias_selecionadas": categorias_selecionadas,
            "especialidades_medicas": ESPECIALIDADES_MEDICAS,
            "erro": mensagem,
        },
    )


def _buscar_ou_criar_categoria(db, nome: str):
    """Reaproveita a categoria se já existir uma com o mesmo nome
    (ignorando maiúsculas/minúsculas), pra não criar duplicada."""
    nome = nome.strip()
    if not nome:
        return None
    categoria = db.query(models.Category).filter(
        func.lower(models.Category.nome) == nome.lower()
    ).first()
    if not categoria:
        categoria = models.Category(nome=nome)
        db.add(categoria)
        db.commit()
        db.refresh(categoria)
    return categoria


def _aplicar_dados_perfil(
    perfil, db, *, estado, cidade, bairro, endereco, atende_domicilio, descricao,
    valor_mao_de_obra, whatsapp, categorias_ids, outra_categoria,
    crm, especialidade_medica, especialidade_medica_outra,
    atende_convenio, convenios_aceitos, foto,
    foto_e_logo=None,
    exigir_completo=True,
):
    """
    Aplica os campos do formulário de editar perfil (usado tanto pelo próprio
    profissional quanto pelo admin editando em nome dele). Retorna uma
    mensagem de erro (str) se algum campo obrigatório estiver faltando ou a
    foto for inválida, ou None se salvou certo.

    exigir_completo=False (usado só pelo admin) pula as exigências de
    bairro/WhatsApp/categoria/foto -- necessário pra corrigir perfis criados
    via indicação, que nascem incompletos de propósito (a indicação não
    coleta foto nem bairro) e ficariam impossíveis de editar aos poucos se
    cada correção precisasse vir com o cadastro inteiro completo.
    """
    estado = (estado or "").strip().upper()
    cidade = cidade.strip()
    bairro = bairro.strip()
    whatsapp = whatsapp.strip()

    if not cidade:
        return "Cidade é obrigatória."
    if exigir_completo and not estado:
        return "Estado é obrigatório."
    if exigir_completo:
        if not bairro:
            return "Bairro é obrigatório."
        if not whatsapp:
            return "WhatsApp para contato é obrigatório."
        # Sem isso, um profissional conseguia terminar o cadastro sem escolher
        # nenhuma área de atuação -- e o admin não tinha como saber o que essa
        # pessoa faz na hora de decidir se aprova ou não.
        if not categorias_ids and not outra_categoria.strip():
            return "Selecione pelo menos uma categoria (ou digite em \"Outra categoria\")."

    tem_foto_nova = bool(foto and foto.filename)
    foto_nova_transparente = False
    if exigir_completo and not tem_foto_nova and not perfil.foto_url:
        return "Foto é obrigatória."

    if tem_foto_nova:
        if foto.content_type not in EXTENSOES_FOTO_PERMITIDAS:
            return "Formato de foto não suportado. Envie uma imagem JPG, PNG ou WEBP."
        conteudo = foto.file.read()
        if len(conteudo) > TAMANHO_MAXIMO_FOTO:
            return "A foto é muito grande (máximo 5 MB)."
        conteudo_normalizado = _validar_e_normalizar_foto(conteudo, foto.content_type)
        if conteudo_normalizado is None:
            return "O arquivo enviado não é uma imagem válida. Tente outra foto."
        extensao = EXTENSOES_FOTO_PERMITIDAS[foto.content_type]
        nome_arquivo = f"profissional_{perfil.usuario_id}{extensao}"
        perfil.foto_url = storage.salvar_foto(conteudo_normalizado, nome_arquivo, foto.content_type)
        # Olha os bytes ORIGINAIS: a normalização pra JPEG descarta a
        # transparência, que é justamente o que interessa aqui.
        foto_nova_transparente = _imagem_tem_transparencia(conteudo)

    # A caixinha do formulário manda. Imagem com fundo transparente liga
    # sozinha, porque recortada ela apareceria vazada de qualquer jeito.
    perfil.foto_e_logo = bool(foto_e_logo) or foto_nova_transparente

    perfil.estado = estado or None
    perfil.cidade = cidade
    perfil.bairro = bairro
    perfil.endereco = endereco.strip()
    perfil.atende_domicilio = bool(atende_domicilio)
    perfil.descricao = descricao.strip()
    perfil.valor_mao_de_obra = valor_mao_de_obra.strip()
    perfil.whatsapp = whatsapp

    # Campos de médico — só têm efeito visível se a categoria "Médico"
    # também estiver marcada (ver property `eh_medico`), mas salvamos o
    # que veio no formulário de qualquer forma.
    perfil.crm = crm.strip() or None
    especialidade_final = (
        especialidade_medica_outra.strip()
        if especialidade_medica == "_outra"
        else especialidade_medica.strip()
    )
    perfil.especialidade_medica = especialidade_final or None
    if atende_convenio == "sim":
        perfil.atende_convenio = True
    elif atende_convenio == "nao":
        perfil.atende_convenio = False
    else:
        perfil.atende_convenio = None
    perfil.convenios_aceitos = convenios_aceitos.strip() or None

    categorias_escolhidas = db.query(models.Category).filter(
        models.Category.id.in_(categorias_ids)
    ).all()

    categoria_nova = _buscar_ou_criar_categoria(db, outra_categoria)
    if categoria_nova and categoria_nova not in categorias_escolhidas:
        categorias_escolhidas.append(categoria_nova)

    perfil.categorias = categorias_escolhidas

    db.commit()
    db.refresh(perfil)
    return None


@app.post("/profissional/perfil/editar")
def salvar_perfil(
    request: Request,
    estado: str = Form(""),
    cidade: str = Form(...),
    bairro: str = Form(""),
    endereco: str = Form(""),
    atende_domicilio: str | None = Form(None),
    descricao: str = Form(""),
    valor_mao_de_obra: str = Form(""),
    whatsapp: str = Form(""),
    categorias_ids: list[int] = Form([]),
    outra_categoria: str = Form(""),
    crm: str = Form(""),
    especialidade_medica: str = Form(""),
    especialidade_medica_outra: str = Form(""),
    atende_convenio: str | None = Form(None),
    convenios_aceitos: str = Form(""),
    foto_e_logo: str | None = Form(None),
    foto: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not usuario or usuario.tipo != "profissional":
        return RedirectResponse("/login", status_code=303)

    perfil = usuario.perfil_profissional
    if not perfil:
        perfil = models.ProfessionalProfile(usuario_id=usuario.id, cidade="")
        db.add(perfil)

    erro_msg = _aplicar_dados_perfil(
        perfil, db, estado=estado, cidade=cidade, bairro=bairro, endereco=endereco,
        atende_domicilio=atende_domicilio, descricao=descricao,
        valor_mao_de_obra=valor_mao_de_obra, whatsapp=whatsapp,
        categorias_ids=categorias_ids, outra_categoria=outra_categoria,
        crm=crm, especialidade_medica=especialidade_medica,
        especialidade_medica_outra=especialidade_medica_outra,
        atende_convenio=atende_convenio, convenios_aceitos=convenios_aceitos,
        foto_e_logo=foto_e_logo,
        foto=foto,
    )
    if erro_msg:
        return _erro_editar_perfil(request, db, usuario, perfil, erro_msg)

    return RedirectResponse(f"/profissional/{perfil.id}", status_code=303)


# ---------------------------------------------------------------------------
# Painel admin (aprovar profissionais)
# ---------------------------------------------------------------------------

def eh_admin(usuario) -> bool:
    return bool(usuario) and _eh_email_admin(usuario.email)


def eh_dono(usuario) -> bool:
    return bool(usuario) and _eh_email_dono(usuario.email)


@app.get("/admin")
def admin_painel(
    request: Request,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)

    pendentes = (
        db.query(models.ProfessionalProfile)
        .filter(
            models.ProfessionalProfile.aprovado == False,  # noqa: E712
            models.ProfessionalProfile.usuario.has(models.User.tipo == "profissional"),
        )
        .all()
    )
    aprovados = db.query(models.ProfessionalProfile).filter(
        models.ProfessionalProfile.aprovado == True,  # noqa: E712
        models.ProfessionalProfile.usuario.has(models.User.tipo == "profissional"),
    ).all()
    indicacoes_pendentes = db.query(models.Indicacao).filter(
        models.Indicacao.status == "pendente"
    ).order_by(models.Indicacao.criado_em.desc()).all()
    indicacoes_contatadas = db.query(models.Indicacao).filter(
        models.Indicacao.status == "contatada"
    ).order_by(models.Indicacao.criado_em.desc()).all()
    indicacoes_autenticadas = db.query(models.Indicacao).filter(
        models.Indicacao.status == "autenticada"
    ).order_by(models.Indicacao.criado_em.desc()).all()
    # Indicações autenticadas antes dessa função criar o profissional de
    # verdade (versão anterior só marcava o status) ficaram "presas" sem
    # perfil -- usamos isso pra saber quais ainda precisam do botão de
    # publicar de novo.
    telefones_ja_profissionais = {
        linha[0] for linha in db.query(models.User.telefone).filter(models.User.tipo == "profissional").all()
    }

    clientes = db.query(models.User).filter(
        models.User.tipo == "cliente"
    ).order_by(models.User.criado_em.desc()).all()
    # Contas admin aparecem primeiro na lista, independente da data de
    # cadastro (sort estável preserva a ordem por criado_em dentro de cada grupo).
    clientes.sort(key=lambda c: c.email not in ADMIN_EMAILS)

    categorias = db.query(models.Category).order_by(models.Category.nome).all()
    grupos_categorias = sorted({c.grupo for c in categorias if c.grupo})
    categorias_por_grupo = _categorias_agrupadas(db)

    # --- Números do site ---------------------------------------------------
    sete_dias_atras = datetime.utcnow() - timedelta(days=7)
    total_avaliacoes = db.query(models.Review).count()
    media_geral = db.query(func.avg(models.Review.estrelas)).scalar()
    novos_cadastros_7d = db.query(models.User).filter(
        models.User.criado_em >= sete_dias_atras
    ).count()
    categorias_populares = (
        db.query(models.Category.nome, func.count(models.ProfessionalProfile.id))
        .join(models.professional_categories, models.Category.id == models.professional_categories.c.category_id)
        .join(models.ProfessionalProfile, models.ProfessionalProfile.id == models.professional_categories.c.professional_id)
        .filter(models.ProfessionalProfile.aprovado == True)  # noqa: E712
        .group_by(models.Category.nome)
        .order_by(func.count(models.ProfessionalProfile.id).desc())
        .limit(5)
        .all()
    )

    numeros = {
        "total_clientes": len(clientes),
        "total_profissionais_aprovados": len(aprovados),
        "total_profissionais_pendentes": len(pendentes),
        "total_avaliacoes": total_avaliacoes,
        "media_geral": round(media_geral, 1) if media_geral else None,
        "total_indicacoes_pendentes": len(indicacoes_pendentes),
        "total_indicacoes_contatadas": len(indicacoes_contatadas),
        "total_indicacoes_autenticadas": len(indicacoes_autenticadas),
        "novos_cadastros_7d": novos_cadastros_7d,
        "categorias_populares": categorias_populares,
    }

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "usuario": usuario,
            "pendentes": pendentes,
            "aprovados": aprovados,
            "indicacoes_pendentes": indicacoes_pendentes,
            "indicacoes_contatadas": indicacoes_contatadas,
            "indicacoes_autenticadas": indicacoes_autenticadas,
            "telefones_ja_profissionais": telefones_ja_profissionais,
            "clientes": clientes,
            "categorias": categorias,
            "categorias_por_grupo": categorias_por_grupo,
            "grupos_categorias": grupos_categorias,
            "emails_admin": ADMIN_EMAILS,
            "numeros": numeros,
            "eh_dono_usuario": eh_dono(usuario),
        },
    )


# ---------------------------------------------------------------------------
# Painel do Dono (acima do admin comum -- só quem é DONO_EMAIL, ver eh_dono)
# ---------------------------------------------------------------------------

def _crescimento_mensal(db: Session) -> list[tuple[str, int]]:
    """Total de usuários cadastrados, acumulado mês a mês, pro gráfico de
    crescimento do Painel do Dono."""
    datas = [
        linha[0] for linha in db.query(models.User.criado_em).filter(models.User.criado_em.isnot(None)).all()
    ]
    por_mes: dict[str, int] = {}
    for data in datas:
        chave = data.strftime("%Y-%m")
        por_mes[chave] = por_mes.get(chave, 0) + 1

    acumulado = []
    total = 0
    for mes in sorted(por_mes):
        total += por_mes[mes]
        acumulado.append((mes, total))
    return acumulado


def _origem_cadastro(usuario: "models.User") -> str:
    if usuario.tipo == "profissional" and usuario.perfil_profissional and usuario.perfil_profissional.criado_via_indicacao:
        return "indicacao"
    if usuario.google_id:
        return "google"
    return "direto"


ORIGEM_ROTULOS = {"direto": "Cadastro direto", "google": "Google", "indicacao": "Indicação (admin)"}


@app.get("/dono")
def dono_painel(
    request: Request,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_dono(usuario):
        return RedirectResponse("/admin", status_code=303)

    total_usuarios = db.query(models.User).count()
    total_clientes = db.query(models.User).filter(models.User.tipo == "cliente").count()
    total_profissionais = db.query(models.User).filter(models.User.tipo == "profissional").count()
    total_aprovados = db.query(models.ProfessionalProfile).filter(
        models.ProfessionalProfile.aprovado == True  # noqa: E712
    ).count()
    total_pendentes_aprovacao = db.query(models.ProfessionalProfile).filter(
        models.ProfessionalProfile.aprovado == False  # noqa: E712
    ).count()
    total_bloqueados = db.query(models.User).filter(models.User.ativo == False).count()  # noqa: E712
    sete_dias_atras = datetime.utcnow() - timedelta(days=7)
    novos_7d = db.query(models.User).filter(models.User.criado_em >= sete_dias_atras).count()
    total_indicacoes_pendentes = db.query(models.Indicacao).filter(models.Indicacao.status == "pendente").count()

    atividade_recente = (
        db.query(models.User).order_by(models.User.criado_em.desc()).limit(8).all()
    )

    return templates.TemplateResponse(
        "dono.html",
        {
            "request": request,
            "usuario": usuario,
            "eh_dono_usuario": True,
            "numeros": {
                "total_usuarios": total_usuarios,
                "total_clientes": total_clientes,
                "total_profissionais": total_profissionais,
                "total_aprovados": total_aprovados,
                "total_pendentes_aprovacao": total_pendentes_aprovacao,
                "total_bloqueados": total_bloqueados,
                "novos_7d": novos_7d,
                "total_indicacoes_pendentes": total_indicacoes_pendentes,
            },
            "crescimento_mensal": _crescimento_mensal(db),
            "atividade_recente": atividade_recente,
        },
    )


@app.get("/dono/usuarios")
def dono_usuarios(
    request: Request,
    busca: str = "",
    tipo: str = "",
    status: str = "",
    aprovacao: str = "",
    origem: str = "",
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_dono(usuario):
        return RedirectResponse("/admin", status_code=303)

    query = db.query(models.User)
    busca = busca.strip()
    if busca:
        query = query.filter(or_(
            models.User.nome.ilike(f"%{busca}%"),
            models.User.email.ilike(f"%{busca}%"),
        ))
    if tipo in ("cliente", "profissional"):
        query = query.filter(models.User.tipo == tipo)
    if status == "ativo":
        query = query.filter(models.User.ativo == True)  # noqa: E712
    elif status == "bloqueado":
        query = query.filter(models.User.ativo == False)  # noqa: E712
    elif status == "email_nao_confirmado":
        query = query.filter(models.User.email_verificado == False)  # noqa: E712

    usuarios = query.order_by(models.User.criado_em.desc()).all()

    if aprovacao in ("pendente", "aprovado", "pausado"):
        def bate_aprovacao(u):
            if u.tipo != "profissional" or not u.perfil_profissional:
                return False
            p = u.perfil_profissional
            if aprovacao == "pendente":
                return not p.aprovado
            if aprovacao == "aprovado":
                return p.aprovado and p.ativo
            return p.aprovado and not p.ativo  # pausado
        usuarios = [u for u in usuarios if bate_aprovacao(u)]

    if origem in ORIGEM_ROTULOS:
        usuarios = [u for u in usuarios if _origem_cadastro(u) == origem]

    return templates.TemplateResponse(
        "dono_usuarios.html",
        {
            "request": request,
            "usuario": usuario,
            "eh_dono_usuario": True,
            "usuarios": usuarios,
            "origem_cadastro": _origem_cadastro,
            "origem_rotulos": ORIGEM_ROTULOS,
            "emails_admin": ADMIN_EMAILS,
            "total_usuarios": db.query(models.User).count(),
            "total_clientes": db.query(models.User).filter(models.User.tipo == "cliente").count(),
            "total_profissionais": db.query(models.User).filter(models.User.tipo == "profissional").count(),
            "total_bloqueados": db.query(models.User).filter(models.User.ativo == False).count(),  # noqa: E712
            "filtro_busca": busca,
            "filtro_tipo": tipo,
            "filtro_status": status,
            "filtro_aprovacao": aprovacao,
            "filtro_origem": origem,
        },
    )


NOTIFICACOES_ADMIN_LIMITE = 20


@app.get("/admin/notificacoes/dados")
def admin_notificacoes_dados(db: Session = Depends(get_db), usuario=Depends(auth.usuario_logado)):
    """Dados pro sino de notificação no menu: últimos cadastros (cliente ou
    profissional), com "novo" = criado depois da última vez que esse admin
    abriu o sino. Consultado via fetch() pelo JS do base.html -- assim o
    sino funciona em toda página, sem precisar que cada rota do site passe
    manualmente `usuario`/`eh_admin_usuario` pro template (a maioria não
    passa hoje)."""
    if not eh_admin(usuario):
        return JSONResponse({"detail": "não autorizado"}, status_code=403)

    visto_em = usuario.notificacoes_vistas_em
    recentes = (
        db.query(models.User)
        .order_by(models.User.criado_em.desc())
        .limit(NOTIFICACOES_ADMIN_LIMITE)
        .all()
    )
    itens = [
        {
            "id": u.id,
            "nome": u.nome,
            "tipo": u.tipo,
            "data_hora": u.criado_em.strftime("%d/%m/%Y %H:%M") if u.criado_em else "",
            "novo": bool(u.criado_em and (visto_em is None or u.criado_em > visto_em)),
        }
        for u in recentes
    ]
    return {"nao_lidas": sum(1 for item in itens if item["novo"]), "itens": itens}


@app.post("/admin/notificacoes/marcar-lidas")
def admin_notificacoes_marcar_lidas(db: Session = Depends(get_db), usuario=Depends(auth.usuario_logado)):
    if not eh_admin(usuario):
        return JSONResponse({"detail": "não autorizado"}, status_code=403)
    usuario.notificacoes_vistas_em = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/admin/categoria/nova")
def admin_categoria_nova(
    nome: str = Form(...),
    grupo: str = Form(""),
    novo_grupo: str = Form(""),
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)

    categoria = _buscar_ou_criar_categoria(db, nome)
    grupo_final = (novo_grupo if grupo == "_novo" else grupo).strip()
    if categoria and grupo_final and not categoria.grupo:
        categoria.grupo = grupo_final
        db.commit()

    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/categoria/{categoria_id}/editar")
def admin_categoria_editar(
    categoria_id: int,
    nome: str = Form(...),
    grupo: str = Form(""),
    novo_grupo: str = Form(""),
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)

    categoria = db.query(models.Category).filter(models.Category.id == categoria_id).first()
    if categoria:
        nome = nome.strip()
        # Não deixa renomear pra um nome que já é de outra categoria --
        # a coluna é unique e isso derrubaria a query com um IntegrityError.
        ja_existe_outra = nome and db.query(models.Category).filter(
            func.lower(models.Category.nome) == nome.lower(),
            models.Category.id != categoria.id,
        ).first()
        if nome and not ja_existe_outra:
            categoria.nome = nome

        grupo_final = (novo_grupo if grupo == "_novo" else grupo).strip()
        categoria.grupo = grupo_final or None
        db.commit()

    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/profissional/{perfil_id}/editar")
def admin_form_editar_perfil(
    perfil_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)

    perfil = db.query(models.ProfessionalProfile).filter(models.ProfessionalProfile.id == perfil_id).first()
    if not perfil:
        return RedirectResponse("/admin", status_code=303)

    categorias_selecionadas = {c.id for c in perfil.categorias}

    return templates.TemplateResponse(
        "editar_perfil.html",
        {
            "request": request,
            "usuario": usuario,
            "perfil": perfil,
            "categorias_por_grupo": _categorias_agrupadas(db),
            "categorias_selecionadas": categorias_selecionadas,
            "especialidades_medicas": ESPECIALIDADES_MEDICAS,
            "erro": None,
            "form_action": f"/admin/profissional/{perfil_id}/editar",
            "admin_editando": perfil.usuario,
        },
    )


@app.post("/admin/profissional/{perfil_id}/editar")
def admin_salvar_perfil(
    perfil_id: int,
    request: Request,
    estado: str = Form(""),
    cidade: str = Form(...),
    bairro: str = Form(""),
    endereco: str = Form(""),
    atende_domicilio: str | None = Form(None),
    descricao: str = Form(""),
    valor_mao_de_obra: str = Form(""),
    whatsapp: str = Form(""),
    categorias_ids: list[int] = Form([]),
    outra_categoria: str = Form(""),
    crm: str = Form(""),
    especialidade_medica: str = Form(""),
    especialidade_medica_outra: str = Form(""),
    atende_convenio: str | None = Form(None),
    convenios_aceitos: str = Form(""),
    foto_e_logo: str | None = Form(None),
    tipo_perfil: str = Form("professional"),
    foto: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)

    perfil = db.query(models.ProfessionalProfile).filter(models.ProfessionalProfile.id == perfil_id).first()
    if not perfil:
        return RedirectResponse("/admin", status_code=303)

    # Classificação interna (profissional / empresa / administração). Não
    # afeta busca nem destaque no catálogo -- só registra o que o cadastro é.
    if tipo_perfil in ("professional", "company", "admin"):
        perfil.tipo_perfil = tipo_perfil

    erro_msg = _aplicar_dados_perfil(
        perfil, db, estado=estado, cidade=cidade, bairro=bairro, endereco=endereco,
        atende_domicilio=atende_domicilio, descricao=descricao,
        valor_mao_de_obra=valor_mao_de_obra, whatsapp=whatsapp,
        categorias_ids=categorias_ids, outra_categoria=outra_categoria,
        crm=crm, especialidade_medica=especialidade_medica,
        especialidade_medica_outra=especialidade_medica_outra,
        atende_convenio=atende_convenio, convenios_aceitos=convenios_aceitos,
        foto_e_logo=foto_e_logo,
        foto=foto, exigir_completo=False,
    )
    if erro_msg:
        categorias_selecionadas = {c.id for c in perfil.categorias}
        return templates.TemplateResponse(
            "editar_perfil.html",
            {
                "request": request,
                "usuario": usuario,
                "perfil": perfil,
                "categorias_por_grupo": _categorias_agrupadas(db),
                "categorias_selecionadas": categorias_selecionadas,
                "especialidades_medicas": ESPECIALIDADES_MEDICAS,
                "erro": erro_msg,
                "form_action": f"/admin/profissional/{perfil_id}/editar",
                "admin_editando": perfil.usuario,
            },
        )

    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/usuario/{usuario_id}/editar")
def admin_form_editar_usuario(
    usuario_id: int,
    request: Request,
    salvo: str | None = None,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)

    alvo = db.query(models.User).filter(models.User.id == usuario_id).first()
    if not alvo:
        return RedirectResponse("/admin", status_code=303)

    return templates.TemplateResponse(
        "admin_editar_usuario.html",
        {
            "request": request,
            "usuario": usuario,
            "eh_admin_usuario": True,
            "eh_dono_usuario": eh_dono(usuario),
            "alvo": alvo,
            "eh_conta_admin": _eh_email_admin(alvo.email),
            "erro": None,
            "salvo": bool(salvo),
        },
    )


@app.post("/admin/usuario/{usuario_id}/editar")
def admin_salvar_usuario(
    usuario_id: int,
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
    telefone: str = Form(...),
    cidade: str = Form(""),
    bairro: str = Form(""),
    tipo: str = Form("cliente"),
    email_verificado: str | None = Form(None),
    ativo: str | None = Form(None),
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    """Edita os dados de cadastro de qualquer conta (cliente ou profissional).

    É a tela que resolve o caso mais comum do dia a dia: telefone digitado
    errado ou repetido, e-mail trocado, pessoa que não recebeu a confirmação.
    O perfil público do profissional continua sendo editado em
    /admin/profissional/<id>/editar -- aqui são só os dados da conta.
    """
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)

    alvo = db.query(models.User).filter(models.User.id == usuario_id).first()
    if not alvo:
        return RedirectResponse("/admin", status_code=303)

    eh_conta_admin = _eh_email_admin(alvo.email)

    def erro(mensagem):
        return templates.TemplateResponse(
            "admin_editar_usuario.html",
            {
                "request": request,
                "usuario": usuario,
                "eh_admin_usuario": True,
                "eh_dono_usuario": eh_dono(usuario),
                "alvo": alvo,
                "eh_conta_admin": eh_conta_admin,
                "erro": mensagem,
                "salvo": False,
            },
        )

    nome = nome.strip()
    email = email.strip().lower()
    telefone = telefone.strip()

    if not nome:
        return erro("O nome não pode ficar em branco.")
    if not EMAIL_REGEX.match(email):
        return erro("Digite um e-mail válido.")
    if not telefone:
        return erro("O telefone não pode ficar em branco.")

    # Uma conta é admin pelo e-mail (lista ADMIN_EMAIL, do ambiente). Deixar
    # trocar o e-mail aqui tiraria o acesso ao painel na hora, sem jeito de
    # voltar pela interface -- então esse campo fica congelado.
    if eh_conta_admin and email != alvo.email:
        return erro(
            "Essa conta está na lista ADMIN_EMAIL. Ajuste a variável de ambiente "
            "antes de mudar o e-mail, senão ela perde o acesso ao painel."
        )

    # Diferente do cadastro público (que dá uma mensagem genérica de propósito,
    # pra ninguém descobrir quem tem conta), aqui o admin precisa saber
    # exatamente qual campo bateu -- e com quem.
    outro_email = db.query(models.User).filter(
        models.User.email == email, models.User.id != alvo.id
    ).first()
    if outro_email:
        return erro(f"O e-mail {email} já é usado pela conta de {outro_email.nome}.")

    outro_telefone = db.query(models.User).filter(
        models.User.telefone == telefone, models.User.id != alvo.id
    ).first()
    if outro_telefone:
        return erro(
            f"O telefone {telefone} já é usado pela conta de {outro_telefone.nome} "
            f"({outro_telefone.email})."
        )

    if tipo not in ("cliente", "profissional"):
        tipo = alvo.tipo

    alvo.nome = nome
    alvo.email = email
    alvo.telefone = telefone
    alvo.cidade = cidade.strip() or None
    alvo.bairro = bairro.strip() or None
    alvo.email_verificado = email_verificado is not None
    if not eh_conta_admin:  # o admin não consegue se autobloquear
        alvo.ativo = ativo is not None

    if tipo != alvo.tipo:
        if tipo == "profissional":
            # Mesmo caminho do cadastro normal: perfil em branco, esperando
            # o profissional preencher e o admin aprovar.
            if not alvo.perfil_profissional:
                db.add(models.ProfessionalProfile(usuario_id=alvo.id, cidade=""))
        elif alvo.perfil_profissional:
            # Voltando pra cliente, o perfil NÃO é apagado (isso levaria junto
            # as avaliações que os clientes deixaram) -- só sai do catálogo.
            alvo.perfil_profissional.ativo = False
            alvo.perfil_profissional.aprovado = False
        alvo.tipo = tipo

    db.commit()
    return RedirectResponse(f"/admin/usuario/{alvo.id}/editar?salvo=1", status_code=303)


@app.post("/admin/usuario/{usuario_id}/bloquear")
def admin_bloquear_usuario(
    usuario_id: int,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)
    alvo = db.query(models.User).filter(models.User.id == usuario_id).first()
    if alvo and not _eh_email_admin(alvo.email):  # nenhum admin bloqueia outro (nem a si)
        alvo.ativo = not alvo.ativo
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/usuario/{usuario_id}/excluir")
def admin_excluir_usuario(
    usuario_id: int,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)
    alvo = db.query(models.User).filter(models.User.id == usuario_id).first()
    if alvo and not _eh_email_admin(alvo.email):  # nenhum admin exclui outro (nem a si)
        db.delete(alvo)
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/reanalisar-fotos")
def admin_reanalisar_fotos(
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    """Marca como logomarca as imagens de fundo transparente já cadastradas.

    Imagens novas já são reconhecidas no upload; esta ação alcança as que
    subiram antes disso. Baixa cada uma (do R2 ou do disco) e liga o campo
    quando encontra transparência. Nunca DESmarca nada: quem foi marcado na
    mão pelo formulário continua marcado. Imagem fora do ar é ignorada --
    nada além desse campo é tocado.
    """
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)

    import urllib.request

    perfis = db.query(models.ProfessionalProfile).filter(
        models.ProfessionalProfile.foto_url.isnot(None),
        models.ProfessionalProfile.foto_url != "",
    ).all()

    for perfil in perfis:
        url = perfil.foto_url
        try:
            if url.startswith("/static/"):
                caminho = os.path.join(BASE_DIR, url.replace("/static/", "static/", 1))
                with open(caminho, "rb") as arquivo:
                    conteudo = arquivo.read()
            else:
                # O R2 recusa (403) requisição sem User-Agent de navegador.
                pedido = urllib.request.Request(url, headers={"User-Agent": "SocorreAqui/1.0"})
                with urllib.request.urlopen(pedido, timeout=10) as resposta:
                    conteudo = resposta.read(TAMANHO_MAXIMO_FOTO + 1)
        except Exception:
            continue  # imagem fora do ar ou caminho quebrado: deixa como está
        if _imagem_tem_transparencia(conteudo):
            perfil.foto_e_logo = True

    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/aprovar/{perfil_id}")
def admin_aprovar(
    perfil_id: int,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)
    perfil = db.query(models.ProfessionalProfile).filter(models.ProfessionalProfile.id == perfil_id).first()
    if perfil:
        perfil.aprovado = True
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/recusar/{perfil_id}")
def admin_recusar(
    perfil_id: int,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)
    perfil = db.query(models.ProfessionalProfile).filter(models.ProfessionalProfile.id == perfil_id).first()
    if perfil and not perfil.aprovado:
        # Recusa = remove o cadastro inteiro (perfil pendente costuma ser
        # spam/perfil falso). Se for um profissional de verdade, ele pode
        # se cadastrar de novo.
        db.delete(perfil.usuario)
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/pausar/{perfil_id}")
def admin_pausar(
    perfil_id: int,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)
    perfil = db.query(models.ProfessionalProfile).filter(models.ProfessionalProfile.id == perfil_id).first()
    if perfil:
        perfil.ativo = not perfil.ativo
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/indicacao/{indicacao_id}/categoria")
def admin_indicacao_categoria(
    indicacao_id: int,
    categoria_id: str = Form(""),
    nova_categoria: str = Form(""),
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)

    indicacao = db.query(models.Indicacao).filter(models.Indicacao.id == indicacao_id).first()
    if indicacao:
        if nova_categoria.strip():
            categoria = _buscar_ou_criar_categoria(db, nova_categoria)
            indicacao.categoria_id = categoria.id if categoria else None
        elif categoria_id:
            indicacao.categoria_id = int(categoria_id)
        else:
            indicacao.categoria_id = None
        db.commit()

    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/indicacao/{indicacao_id}/contatado")
def admin_indicacao_contatado(
    indicacao_id: int,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)
    indicacao = db.query(models.Indicacao).filter(models.Indicacao.id == indicacao_id).first()
    if indicacao:
        indicacao.status = "contatada"
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/indicacao/{indicacao_id}/autenticar")
def admin_indicacao_autenticar(
    indicacao_id: int,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)
    indicacao = db.query(models.Indicacao).filter(models.Indicacao.id == indicacao_id).first()
    if not indicacao:
        return RedirectResponse("/admin", status_code=303)

    # "Autenticar" publica o profissional direto no catálogo, sem ele
    # precisar se cadastrar sozinho -- útil pra quando o admin já confirmou
    # por telefone que a pessoa existe e topa aparecer. Se já existir conta
    # com esse telefone (a pessoa se cadastrou por conta própria nesse
    # meio-tempo, por exemplo), só marca a indicação como autenticada e
    # não cria duplicado.
    ja_tem_conta = db.query(models.User).filter(
        models.User.telefone == indicacao.telefone_profissional
    ).first()
    if not ja_tem_conta:
        telefone_digitos = re.sub(r"\D", "", indicacao.telefone_profissional) or str(indicacao.id)
        novo_usuario = models.User(
            nome=indicacao.nome_profissional,
            email=f"indicacao-{telefone_digitos}@sem-email.socorreaqui.com.br",
            telefone=indicacao.telefone_profissional,
            senha_hash=None,
            tipo="profissional",
            estado=indicacao.estado,
            cidade=indicacao.cidade,
            email_verificado=False,
            ativo=True,
        )
        db.add(novo_usuario)
        db.commit()
        db.refresh(novo_usuario)

        perfil = models.ProfessionalProfile(
            usuario_id=novo_usuario.id,
            estado=indicacao.estado,
            cidade=indicacao.cidade or "",
            aprovado=True,
            ativo=True,
            criado_via_indicacao=True,
        )
        if indicacao.categoria:
            perfil.categorias.append(indicacao.categoria)
        db.add(perfil)

    indicacao.status = "autenticada"
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/indicacao/{indicacao_id}/descartar")
def admin_indicacao_descartar(
    indicacao_id: int,
    db: Session = Depends(get_db),
    usuario=Depends(auth.usuario_logado),
):
    if not eh_admin(usuario):
        return RedirectResponse("/", status_code=303)
    indicacao = db.query(models.Indicacao).filter(models.Indicacao.id == indicacao_id).first()
    if indicacao:
        db.delete(indicacao)
        db.commit()
    return RedirectResponse("/admin", status_code=303)
