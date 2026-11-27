"""Textos institucionais do SocorreAqui: Sobre, Contato, Termos de uso e
Política de privacidade.

ATENÇÃO: os textos de Termos e Privacidade são MINUTAS. Eles descrevem o que
o sistema faz hoje, mas precisam de aprovação do proprietário antes da
publicação definitiva. Enquanto MINUTA_EM_REVISAO for True, cada página mostra
um aviso de minuta.

Dados que não existem no projeto ficam entre colchetes, como
[a informar]. Nenhum CNPJ, endereço, e-mail, responsável legal ou prazo de
retenção foi inventado.
"""

MINUTA_EM_REVISAO = True

A_INFORMAR = "[a informar]"

PAGINAS = {
    "sobre": {
        "titulo": "Sobre o SocorreAqui",
        "descricao": "Conheça o SocorreAqui, o marketplace de serviços locais.",
        "minuta": False,
        "secoes": [
            ("O que é", [
                "O SocorreAqui conecta pessoas que precisam de um serviço do dia a dia "
                "a profissionais autônomos da sua região. A busca é por categoria e "
                "cidade, e o contato é feito diretamente com o profissional, pelo WhatsApp.",
            ]),
            ("Como funciona", [
                "Você busca um serviço, compara perfis com avaliações de clientes e "
                "fala diretamente com quem vai atender. O SocorreAqui não contrata, "
                "não executa e não garante os serviços oferecidos pelos profissionais.",
            ]),
            ("Onde atuamos", [
                "O catálogo é organizado por estado e cidade e aceita profissionais de todo o Brasil. "
                "A cobertura de cada cidade cresce conforme os cadastros chegam.",
            ]),
            ("Quem desenvolve", [
                "O SocorreAqui é desenvolvido pela H2 Sistemas.",
            ]),
        ],
    },
    "contato": {
        "titulo": "Contato",
        "descricao": "Fale com a equipe do SocorreAqui.",
        "minuta": False,
        "secoes": [
            ("Canais de contato", [
                "E-mail: " + A_INFORMAR + ".",
                "WhatsApp: " + A_INFORMAR + ".",
            ]),
            ("Empresa responsável", [
                "Razão social: " + A_INFORMAR + ".",
                "CNPJ: " + A_INFORMAR + ".",
                "Endereço: " + A_INFORMAR + ".",
            ]),
            ("Dúvidas sobre dados pessoais", [
                "Para pedidos relacionados à privacidade (acesso, correção ou exclusão de dados), "
                "use o canal acima. O encarregado pelo tratamento de dados é " + A_INFORMAR + ".",
            ]),
            ("Denúncias e avaliações", [
                "Se encontrar um perfil com informação falsa ou um comportamento inadequado, "
                "envie a descrição pelo canal acima. Ao receber, a equipe avalia o caso e pode "
                "pausar ou bloquear o perfil enquanto analisa.",
            ]),
        ],
    },
    "termos": {
        "titulo": "Termos de uso",
        "descricao": "Regras para usar o SocorreAqui.",
        "minuta": True,
        "secoes": [
            ("1. Aceitação", [
                "Ao usar o SocorreAqui você concorda com estes termos. Se não concordar, não utilize a plataforma.",
            ]),
            ("2. Cadastro", [
                "Para criar uma conta é preciso informar nome, e-mail, telefone e tipo de conta (cliente ou profissional). "
                "As informações devem ser verdadeiras e mantidas atualizadas. A conta é pessoal e a senha não deve ser compartilhada.",
                "Contas criadas com e-mail e senha só ficam ativas depois da confirmação do e-mail.",
            ]),
            ("3. Perfil profissional", [
                "Profissionais que se cadastram são responsáveis pelas informações do perfil, pelos serviços oferecidos "
                "e pelo cumprimento das obrigações legais da própria atividade, incluindo licenças e registros exigidos para cada profissão.",
                "Um perfil só é publicado no catálogo depois de aprovado pela equipe e de ter os campos obrigatórios preenchidos: "
                "nome, categoria principal, estado, cidade, WhatsApp válido e descrição dos serviços.",
                "A equipe pode pausar, recusar ou remover um perfil que contenha informação falsa, enganosa ou que viole estes termos.",
            ]),
            ("4. Relação entre clientes e profissionais", [
                "O SocorreAqui apenas aproxima clientes e profissionais. A contratação, o preço, a execução e a garantia do serviço "
                "são combinados diretamente entre as partes. A plataforma não é parte desse contrato.",
                "Atualmente a plataforma não processa pagamentos entre clientes e profissionais.",
            ]),
            ("5. Avaliações e indicações", [
                "Clientes podem avaliar profissionais com nota de 1 a 5 e comentário. As avaliações devem refletir uma experiência real. "
                "Avaliações falsas ou ofensivas podem ser removidas.",
                "Usuários podem indicar profissionais que ainda não estão cadastrados. A indicação não é uma verificação "
                "de identidade, de qualificação ou de idoneidade feita pela plataforma.",
            ]),
            ("6. Uso permitido", [
                "É proibido usar a plataforma para fraude, spam, divulgação de conteúdo ilegal, coleta automatizada de dados "
                "de outros usuários ou qualquer ação que prejudique o funcionamento do serviço.",
            ]),
            ("7. Suspensão e encerramento", [
                "A conta pode ser bloqueada ou encerrada em caso de violação destes termos. O próprio usuário pode excluir a conta "
                "pela área de dados da sua conta.",
            ]),
            ("8. Responsabilidade", [
                "O SocorreAqui não se responsabiliza pela qualidade, pela pontualidade ou pelo resultado dos serviços contratados "
                "entre clientes e profissionais, nem por danos decorrentes dessas contratações, observados os limites da lei aplicável.",
            ]),
            ("9. Alterações", [
                "Estes termos podem ser atualizados. A versão vigente fica publicada nesta página, com a data da última alteração.",
            ]),
            ("10. Foro e legislação", [
                "Estes termos são regidos pela legislação brasileira. Fica eleito o foro da comarca de " + A_INFORMAR + ", "
                "sem prejuízo dos direitos do consumidor previstos em lei.",
            ]),
        ],
    },
    "privacidade": {
        "titulo": "Política de privacidade",
        "descricao": "Como o SocorreAqui trata os seus dados pessoais.",
        "minuta": True,
        "secoes": [
            ("1. Quem trata os dados", [
                "O controlador dos dados é " + A_INFORMAR + " (razão social e CNPJ conforme a página de Contato). "
                "Dúvidas e pedidos sobre privacidade podem ser enviados pelo canal indicado na página de Contato.",
            ]),
            ("2. Dados que coletamos", [
                "Cadastro: nome, e-mail, telefone, tipo de conta e, quando informados, cidade, estado e bairro.",
                "Perfil profissional: categorias de serviço, descrição, valor do serviço, WhatsApp, endereço (opcional), "
                "foto ou logo e, para profissionais de saúde, dados de registro profissional e convênios aceitos.",
                "Avaliações e indicações: nota, comentário, e os dados de contato da pessoa indicada informados por quem indica.",
                "Login com Google, quando usado: identificador da conta e e-mail fornecidos pelo Google.",
                "Dados técnicos de sessão: um cookie de sessão necessário para manter o login. Em produção, também usamos "
                "Google Analytics para medir o uso geral do site.",
            ]),
            ("3. Para que usamos", [
                "Operar o catálogo, mostrar perfis e permitir o contato entre clientes e profissionais.",
                "Confirmar o e-mail, permitir a redefinição de senha e enviar avisos transacionais da conta.",
                "Aprovar, moderar e proteger a plataforma contra fraudes e abusos.",
            ]),
            ("4. Com quem compartilhamos", [
                "Informações do perfil profissional aparecem publicamente no catálogo, a menos que o perfil esteja incompleto ou pausado.",
                "Telefone e WhatsApp só são exibidos a usuários logados.",
                "Fornecedores técnicos: SendGrid (envio de e-mails), Cloudflare R2 (armazenamento de fotos, quando configurado), "
                "Google (login, quando usado) e Google Analytics (medição de uso, em produção).",
            ]),
            ("5. Por quanto tempo guardamos", [
                "Os dados da conta são mantidos enquanto a conta existir. Os prazos de retenção de cada categoria de dados "
                "ainda precisam ser definidos: " + A_INFORMAR + ".",
            ]),
            ("6. Seus direitos", [
                "Você pode consultar, corrigir e pedir a exclusão dos seus dados. A exclusão da conta pode ser feita pela área "
                "de dados da sua conta. Algumas informações podem ser mantidas quando a lei exigir.",
            ]),
            ("7. Segurança", [
                "Senhas são armazenadas apenas em forma de hash. O acesso administrativo é restrito, e os cookies de sessão são assinados.",
                "Nenhum sistema é totalmente imune a falhas. Em caso de incidente relevante, o titular será informado conforme a lei.",
            ]),
            ("8. Alterações", [
                "Esta política pode ser atualizada. A versão vigente fica publicada nesta página.",
            ]),
        ],
    },
}
