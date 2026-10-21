import type { MarketingMessages } from '../../messages';

// Brazilian Portuguese marketing slice: the apex landing page and "find your
// workspace". Lazy-loaded by `ensureSlices('marketing')` in store.svelte.ts;
// `satisfies MarketingMessages` makes a missing or extra key a compile error.
// Provider names are proper nouns and stay as written. Every figure is
// substantiated in docs/marketing-substantiation.md.
export const messages = {
	'marketing.nav.label': 'Principal',
	'marketing.nav.features': 'Recursos',
	'marketing.nav.how': 'Como funciona',
	'marketing.nav.pricing': 'Preços',
	'marketing.nav.signIn': 'Entrar',
	'marketing.nav.menu': 'Menu',
	'marketing.cta.create': 'Criar espaço de trabalho',
	'marketing.cta.short': 'Comece grátis',

	'marketing.hero.eyebrow': 'Contas a pagar, automatizadas',
	'marketing.hero.title': 'Automação de contas a pagar',
	'marketing.hero.titleAccent': 'que sua equipe financeira vai querer usar.',
	'marketing.hero.lede':
		'Da fatura ao pagamento em minutos, não em dias. A IA lê suas faturas, as aprovações seguem suas regras e as faturas aprovadas são lançadas no seu ERP — com um reembolso típico de 1–2% nos pagamentos a fornecedores que aceitam cartão.',
	'marketing.hero.secondary': 'Veja como funciona',
	'marketing.hero.subNote': 'Grátis para começar · Sem cartão de crédito · Cadastro por conta própria',

	'marketing.demo.status': 'Pronta para revisão',
	'marketing.demo.matched': 'Conciliação em 3 vias',
	'marketing.demo.vendor': 'Fornecedor',
	'marketing.demo.invoiceNo': 'Nº da fatura',
	'marketing.demo.amount': 'Valor',
	'marketing.demo.due': 'Vencimento',
	'marketing.demo.glCode': 'Conta contábil',
	'marketing.demo.glName': 'Custo das mercadorias',
	'marketing.demo.stamp': 'Aprovada · Paga com cartão virtual',
	'marketing.demo.stage.capture': 'Captura',
	'marketing.demo.stage.extract': 'Leitura',
	'marketing.demo.stage.approve': 'Aprovação',
	'marketing.demo.stage.pay': 'Pagamento',

	'marketing.adapters.heading': 'Integrações incluídas',

	'marketing.stats.rails': 'meios de pagamento, do ACH ao CHAPS',
	'marketing.stats.stepTypes': 'tipos de etapa de fluxo de trabalho',
	'marketing.stats.languages': 'idiomas da interface',
	'marketing.stats.rebateValue': '1–2%',
	'marketing.stats.rebate': 'de reembolso típico quando o fornecedor aceita cartão',

	'marketing.features.eyebrow': 'Tudo de que você precisa',
	'marketing.features.heading': 'Uma plataforma, da captura ao pagamento.',
	'marketing.features.lede':
		'A maioria das equipes junta quatro ferramentas para capturar faturas, aprovar, conciliar e lançar no ERP. O FeohLedger faz tudo em um só lugar e funciona com os sistemas que você já usa.',
	'marketing.features.extract.title': 'Leitura de faturas com IA',
	'marketing.features.extract.body':
		'Envie um PDF ou uma foto. A IA lê cada campo e cada item e mostra o grau de certeza de cada um, para sua equipe conferir só o que precisa ser conferido.',
	'marketing.features.approve.title': 'Fluxos de aprovação',
	'marketing.features.approve.body':
		'Direcione as faturas por valor, fornecedor ou conta contábil, sem escrever código. Segregação de funções e trilha de auditoria completa já vêm incluídas, e os aprovadores aprovam pelo Slack, Microsoft Teams, e-mail ou app móvel.',
	'marketing.features.match.title': 'Conciliação com pedido em 2 e 3 vias',
	'marketing.features.match.body':
		'As faturas são conciliadas com pedidos de compra e recebimentos de mercadorias, dentro das tolerâncias que você definir. As divergências vão para a fila de exceções, não para a sua caixa de entrada.',
	'marketing.features.erp.title': 'Sincronização com o ERP nos dois sentidos',
	'marketing.features.erp.body':
		'As faturas aprovadas são lançadas no NetSuite ou no Dynamics 365 Business Central, ou em outros sistemas contábeis pelo Merge.dev. O status do pagamento volta, e o seu ERP continua sendo o sistema de referência.',
	'marketing.features.card.title': 'Pagamentos com cartão virtual',
	'marketing.features.card.body':
		'Pague cada fatura com um cartão virtual de uso único, emitido pela Lithic ou pela Nium, e ganhe um reembolso típico de 1–2% nos pagamentos a fornecedores que aceitam cartão.',
	'marketing.features.exceptions.title': 'Fila de exceções',
	'marketing.features.exceptions.body':
		'Duplicidades, alertas de fraude, valores acima do limite e fornecedores não verificados são sinalizados, atribuídos e resolvidos em uma única fila — sem garimpar o e-mail.',

	'marketing.how.eyebrow': 'Como funciona',
	'marketing.how.heading': 'Da fatura ao razão, em três etapas.',
	'marketing.how.capture.title': 'Captura',
	'marketing.how.capture.body':
		'Encaminhe as faturas por e-mail, envie ou arraste os arquivos, ou tire uma foto no app móvel. PDFs, imagens e papel digitalizado funcionam.',
	'marketing.how.route.title': 'Leitura e encaminhamento',
	'marketing.how.route.body':
		'A IA identifica o fornecedor, o valor, a conta contábil e os itens. Seu fluxo envia a fatura automaticamente para o aprovador certo.',
	'marketing.how.pay.title': 'Pagamento e sincronização',
	'marketing.how.pay.body':
		'As faturas aprovadas são lançadas no seu ERP e agendadas para pagamento por ACH, transferência, cheque ou cartão virtual.',

	'marketing.tally.eyebrow': 'Por que as equipes mudam',
	'marketing.tally.heading': 'Feito para o financeiro, não só vendido para ele.',
	'marketing.tally.lede':
		'O Tesouro inglês quitava uma dívida com um bastão de contagem: um pedaço de madeira entalhado, partido ao meio no comprimento, uma metade para cada parte — a dívida estava paga quando as metades se encaixavam. O trabalho ainda é esse: a fatura contra o pedido, o pagamento contra o extrato. Tudo aqui existe para que as metades se encaixem.',
	'marketing.tally.alt':
		'Um bastão de contagem dourado e entalhado, partido no comprimento em duas metades cujos entalhes se alinham.',
	'marketing.diff.ai.title': 'IA sob o seu controle',
	'marketing.diff.ai.body':
		'Use a nossa IA integrada ou conecte sua própria conta da Claude, OpenAI ou AWS Textract, com suas próprias regras de retenção de dados. Se as faturas nunca puderem sair dos seus servidores, rode a IA neles com o Ollama.',
	'marketing.diff.connect.title': 'Funciona com o que você já usa',
	'marketing.diff.connect.body':
		'Seu ERP, seu banco, seu emissor de cartões e seu e-mail continuam onde estão — o FeohLedger se conecta a cada um. Se trocar algum desses fornecedores depois, suas regras de aprovação e seu histórico continuam no lugar.',
	'marketing.diff.cards.title': 'Cartões que dão retorno',
	'marketing.diff.cards.body':
		'Pagamentos com cartão virtual pela Lithic ou pela Nium rendem um reembolso típico de 1–2%. Se {monthly} por mês em faturas passassem para o cartão, voltariam ao seu orçamento de {low} a {high} por ano.',
	'marketing.diff.cards.note':
		'Exemplo ilustrativo. O reembolso depende da taxa negociada, de quanto do gasto passa para o cartão e de quais fornecedores o aceitam.',
	'marketing.diff.mobile.title': 'Mobile sem concessões',
	'marketing.diff.mobile.body':
		'Apps nativos para iOS e Android com captura pela câmera, login biométrico, aprovação com um deslizar e modo offline. Os aprovadores destravam o contas a pagar até da sala de embarque.',

	'marketing.final.heading': 'Seu espaço de trabalho pronto em minutos.',
	'marketing.final.body':
		'Escolha o endereço do seu espaço de trabalho, confirme seu e-mail e defina sua senha pelo link que enviamos. O plano Free dispensa cartão, contrato e conversa com vendas.',

	'marketing.footer.label': 'Rodapé',
	'marketing.footer.copyright': '© {year} {product}.',

	'marketing.findWorkspace.pageTitle': 'Entrar — {product}',
	'marketing.findWorkspace.heading': 'Entre no seu espaço de trabalho',
	'marketing.findWorkspace.subtitle':
		'Cada empresa entra pelo endereço do próprio espaço de trabalho. Digite o seu e levamos você à página de login dele.',
	'marketing.findWorkspace.label': 'Endereço do espaço de trabalho',
	'marketing.findWorkspace.hint':
		'Ele está no seu e-mail de boas-vindas e no link de qualquer e-mail que o seu espaço de trabalho tenha enviado.',
	'marketing.findWorkspace.submit': 'Continuar',
	'marketing.findWorkspace.going': 'Abrindo {host}…',
	'marketing.findWorkspace.problem.empty': 'Digite o endereço do seu espaço de trabalho.',
	'marketing.findWorkspace.problem.startLetter': 'O endereço de um espaço de trabalho começa com uma letra.',
	'marketing.findWorkspace.problem.characters':
		'Digite só a primeira parte do endereço — letras minúsculas, números e hifens.',
	'marketing.findWorkspace.problem.tooShort': 'O endereço de um espaço de trabalho tem pelo menos 3 caracteres.',
	'marketing.findWorkspace.problem.tooLong': 'O endereço de um espaço de trabalho tem no máximo 30 caracteres.',
	'marketing.findWorkspace.problem.endHyphen': 'O endereço de um espaço de trabalho não pode terminar com hífen.',
	'marketing.findWorkspace.problem.doubleHyphen':
		'O endereço de um espaço de trabalho não pode ter dois hifens seguidos.',
	'marketing.findWorkspace.ownDomain': 'Parece o endereço próprio da sua empresa. {link}',
	'marketing.findWorkspace.ownDomainLink': 'Entrar em {host}',
	'marketing.findWorkspace.configError':
		'Não foi possível carregar os dados de login. Verifique sua conexão e tente de novo.',
	'marketing.findWorkspace.retry': 'Tentar de novo',
	'marketing.findWorkspace.lost':
		'Não encontrou? Pergunte a alguém da equipe que já entra: o endereço aparece na barra de endereços do navegador.',
	'marketing.findWorkspace.noWorkspace': 'Novo no {product}? {create}',
	'marketing.findWorkspace.panelHeading': 'Por que pedimos o endereço',
	'marketing.findWorkspace.panelBody':
		'Cada empresa tem seu próprio espaço de trabalho, com login próprio. Nunca procuramos um espaço de trabalho a partir de um e-mail — assim, ninguém consegue usar esta página para descobrir onde alguém trabalha.'
} satisfies MarketingMessages;
