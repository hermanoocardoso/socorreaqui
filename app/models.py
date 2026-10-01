"""
Modelos (tabelas) do banco de dados.

Estrutura pensada para o MVP:

- User: qualquer pessoa cadastrada (cliente OU profissional). O campo
  `tipo` diz qual é qual. Um profissional também tem uma linha extra
  em ProfessionalProfile com os dados específicos dele.
- Category: categorias de serviço (Eletricista, Vidraceiro, Encanador...).
- ProfessionalProfile: dados do profissional (endereço, valor, descrição).
- Review: avaliação que um cliente deixa para um profissional depois
  de um serviço.
"""
import re
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Text, DateTime,
    ForeignKey, Table
)
from sqlalchemy.orm import relationship

from app.database import Base


# Tabela de associação profissional <-> categorias (um profissional pode
# atender mais de uma categoria, ex: "Eletricista" e "Marido de aluguel")
professional_categories = Table(
    "professional_categories",
    Base.metadata,
    Column("professional_id", Integer, ForeignKey("professional_profiles.id"), primary_key=True),
    Column("category_id", Integer, ForeignKey("categories.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(120), nullable=False)
    email = Column(String(255), nullable=False, unique=True, index=True)
    email_verificado = Column(Boolean, default=False, nullable=False)
    telefone = Column(String(20), nullable=False, unique=True, index=True)  # usado como contato/WhatsApp
    senha_hash = Column(String(255), nullable=True)  # nulo para quem só entra com Google
    google_id = Column(String(64), nullable=True, unique=True, index=True)
    tipo = Column(String(20), nullable=False, default="cliente")  # "cliente" ou "profissional"
    estado = Column(String(2), nullable=True)  # UF de onde a pessoa mora
    cidade = Column(String(120), nullable=True)  # onde a pessoa mora, usado pra agrupar o catálogo por bairro
    bairro = Column(String(120), nullable=True)
    ativo = Column(Boolean, default=True, nullable=False)  # admin pode bloquear o login de uma conta
    criado_em = Column(DateTime, default=datetime.utcnow)
    ultimo_login_em = Column(DateTime, nullable=True)  # atualizado a cada login (senha ou Google)
    # Quando o admin viu pela última vez o sino de notificações de novo
    # cadastro -- só é usado em quem é admin (ver ADMIN_EMAILS em main.py).
    notificacoes_vistas_em = Column(DateTime, nullable=True)

    perfil_profissional = relationship(
        "ProfessionalProfile", back_populates="usuario", uselist=False,
        cascade="all, delete-orphan"
    )
    avaliacoes_feitas = relationship(
        "Review", back_populates="cliente", cascade="all, delete-orphan"
    )
    indicacoes_feitas = relationship(
        "Indicacao", back_populates="indicado_por", cascade="all, delete-orphan"
    )


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(80), nullable=False, unique=True)
    grupo = Column(String(80), nullable=True)  # agrupamento amplo pro menu (ex: "Casa e Reformas")

    profissionais = relationship(
        "ProfessionalProfile", secondary=professional_categories, back_populates="categorias"
    )


class ProfessionalProfile(Base):
    __tablename__ = "professional_profiles"

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)

    estado = Column(String(2), nullable=True)  # UF de onde o profissional atende
    cidade = Column(String(120), nullable=False)
    bairro = Column(String(120), nullable=True)
    endereco = Column(String(255), nullable=True)
    atende_domicilio = Column(Boolean, default=False, nullable=False)  # atende na casa do cliente
    descricao = Column(Text, nullable=True)
    valor_mao_de_obra = Column(String(120), nullable=True)  # texto livre: "A partir de R$ 80" ou "A combinar"
    foto_url = Column(String(255), nullable=True)
    # True quando a imagem enviada é uma logomarca de empresa, e não a foto
    # de uma pessoa. Muda só o enquadramento na tela: foto preenche o quadro
    # (pode cortar as bordas), logo precisa caber inteira -- cortada, o nome
    # da marca some. Preenchido sozinho no upload (ver _imagem_parece_logo).
    foto_e_logo = Column(Boolean, default=False, nullable=False)
    whatsapp = Column(String(20), nullable=True)  # se vazio, usa o telefone do cadastro

    # Campos específicos de médico (só fazem sentido quando a categoria
    # "Médico" está marcada — ver a property `eh_medico` abaixo).
    crm = Column(String(30), nullable=True)
    especialidade_medica = Column(String(120), nullable=True)
    atende_convenio = Column(Boolean, nullable=True)  # None = não informado
    convenios_aceitos = Column(String(255), nullable=True)

    # Diferencia quem é prestador de serviço de quem é empresa/administração da
    # plataforma (ex: a H2 Sistemas, desenvolvedora, cadastrada para testes).
    # "professional" | "company" | "admin". Hoje só serve para NÃO dar destaque
    # visual a quem administra a plataforma -- não altera busca nem filtro.
    tipo_perfil = Column(String(20), nullable=False, default="professional")

    aprovado = Column(Boolean, default=False)  # aprovação manual pelo admin antes de aparecer no catálogo
    ativo = Column(Boolean, default=True)      # profissional pode "pausar" o perfil sem apagar

    # Marca perfis criados pelo admin ao autenticar uma indicação (a pessoa
    # nunca se cadastrou sozinha) -- mostra um selo "Indicado" pra deixar
    # claro que essa recomendação não passou pelo cadastro normal.
    criado_via_indicacao = Column(Boolean, default=False, nullable=False)

    criado_em = Column(DateTime, default=datetime.utcnow)

    usuario = relationship("User", back_populates="perfil_profissional")
    categorias = relationship(
        "Category", secondary=professional_categories, back_populates="profissionais"
    )
    avaliacoes = relationship(
        "Review", back_populates="profissional", cascade="all, delete-orphan"
    )

    @property
    def nota_media(self) -> float:
        if not self.avaliacoes:
            return 0.0
        return round(sum(r.estrelas for r in self.avaliacoes) / len(self.avaliacoes), 1)

    @property
    def total_avaliacoes(self) -> int:
        return len(self.avaliacoes)

    @property
    def whatsapp_numero(self) -> str:
        """Número só com dígitos, pronto para montar o link wa.me/<numero>."""
        bruto = self.whatsapp or self.usuario.telefone or ""
        numero = re.sub(r"\D", "", bruto)
        if not numero.startswith("55"):
            numero = "55" + numero
        return numero

    @property
    def tem_whatsapp(self) -> bool:
        """Só existe botão de WhatsApp se houver algum número cadastrado --
        nem whatsapp próprio, nem telefone do cadastro = não mostra o botão."""
        return bool((self.whatsapp or "").strip() or (self.usuario.telefone or "").strip())

    @property
    def disponibilidade(self) -> str | None:
        """Status real de disponibilidade do profissional.

        AINDA NÃO EXISTE esse dado no cadastro -- por isso retorna sempre None,
        e os templates simplesmente não mostram nenhum selo de disponibilidade.
        Quando o campo passar a existir de verdade, basta esta property devolver
        "agora" | "hoje" | "indisponivel" que a interface já está preparada.
        """
        return None

    @property
    def eh_medico(self) -> bool:
        return any((c.nome or "").strip().lower() == "médico" for c in self.categorias)


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, index=True)
    profissional_id = Column(Integer, ForeignKey("professional_profiles.id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    estrelas = Column(Integer, nullable=False)  # 1 a 5
    comentario = Column(Text, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)

    profissional = relationship("ProfessionalProfile", back_populates="avaliacoes")
    cliente = relationship("User", back_populates="avaliacoes_feitas")


class Indicacao(Base):
    """
    Indicação de um profissional que ainda NÃO está cadastrado no app.
    Qualquer usuário logado (cliente ou profissional) pode indicar alguém
    que conhece; o admin vê a lista em /admin e entra em contato para
    convidar essa pessoa a se cadastrar.
    """
    __tablename__ = "indicacoes"

    id = Column(Integer, primary_key=True, index=True)
    indicado_por_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    nome_profissional = Column(String(120), nullable=False)
    telefone_profissional = Column(String(20), nullable=False)
    categoria_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    estado = Column(String(2), nullable=True)
    cidade = Column(String(120), nullable=True)
    observacao = Column(Text, nullable=True)

    # pendente -> contatada -> (some da lista se descartada)
    status = Column(String(20), nullable=False, default="pendente")
    criado_em = Column(DateTime, default=datetime.utcnow)

    indicado_por = relationship("User", back_populates="indicacoes_feitas")
    categoria = relationship("Category")
