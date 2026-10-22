import type { MarketingMessages } from '../../messages';

// Spanish marketing slice: the apex landing page and "find your workspace".
// Lazy-loaded by `ensureSlices('marketing')` in store.svelte.ts; `satisfies
// MarketingMessages` makes a missing or extra key a compile error. Provider
// names are proper nouns and stay as written. Every figure is substantiated in
// docs/marketing-substantiation.md.
export const messages = {
	'marketing.nav.label': 'Principal',
	'marketing.nav.features': 'Funciones',
	'marketing.nav.how': 'Cómo funciona',
	'marketing.nav.pricing': 'Precios',
	'marketing.nav.signIn': 'Iniciar sesión',
	'marketing.nav.menu': 'Menú',
	'marketing.cta.create': 'Crear espacio de trabajo',
	'marketing.cta.short': 'Empezar gratis',

	'marketing.hero.eyebrow': 'Cuentas por pagar, automatizadas',
	'marketing.hero.title': 'Automatización de cuentas por pagar',
	'marketing.hero.titleAccent': 'que tu equipo financiero sí va a usar.',
	'marketing.hero.lede':
		'De la factura al pago en minutos, no en días. La IA lee tus facturas, las aprobaciones siguen tus reglas y las facturas aprobadas se contabilizan en tu ERP, con un reembolso típico del 1–2 % en los pagos a proveedores que aceptan tarjeta.',
	'marketing.hero.secondary': 'Ver cómo funciona',
	'marketing.hero.subNote': 'Gratis para empezar · Sin tarjeta de crédito · Alta por tu cuenta',

	'marketing.demo.status': 'Lista para revisión',
	'marketing.demo.matched': 'Conciliación a 3 vías',
	'marketing.demo.vendor': 'Proveedor',
	'marketing.demo.invoiceNo': 'N.º de factura',
	'marketing.demo.amount': 'Importe',
	'marketing.demo.due': 'Vence',
	'marketing.demo.glCode': 'Cuenta contable',
	'marketing.demo.glName': 'Coste de ventas',
	'marketing.demo.stamp': 'Aprobada · Pagada con tarjeta virtual',
	'marketing.demo.stage.capture': 'Captura',
	'marketing.demo.stage.extract': 'Lectura',
	'marketing.demo.stage.approve': 'Aprobación',
	'marketing.demo.stage.pay': 'Pago',

	'marketing.adapters.heading': 'Integraciones incluidas',

	'marketing.stats.rails': 'métodos de pago, de ACH a CHAPS',
	'marketing.stats.stepTypes': 'tipos de pasos de flujo de trabajo',
	'marketing.stats.languages': 'idiomas de la interfaz',
	'marketing.stats.rebateValue': '1–2 %',
	'marketing.stats.rebate': 'de reembolso típico si el proveedor acepta tarjeta',

	'marketing.features.eyebrow': 'Todo lo que necesitas',
	'marketing.features.heading': 'Una plataforma, de la captura al pago.',
	'marketing.features.lede':
		'La mayoría de los equipos combinan cuatro herramientas para capturar facturas, aprobarlas, conciliarlas y contabilizarlas en el ERP. FeohLedger lo hace todo en un solo lugar y funciona con los sistemas que ya usas.',
	'marketing.features.extract.title': 'Lectura de facturas con IA',
	'marketing.features.extract.body':
		'Sube un PDF o una foto. La IA lee cada campo y cada línea, e indica su grado de certeza en cada uno, para que tu equipo revise solo lo que hace falta revisar.',
	'marketing.features.approve.title': 'Flujos de aprobación',
	'marketing.features.approve.body':
		'Dirige las facturas según importe, proveedor o cuenta contable, sin escribir código. La segregación de funciones y un registro de auditoría completo vienen incluidos, y los aprobadores firman desde Slack, Microsoft Teams, el correo o la app móvil.',
	'marketing.features.match.title': 'Conciliación con OC a 2 y 3 vías',
	'marketing.features.match.body':
		'Las facturas se concilian con las órdenes de compra y las recepciones de mercancía, dentro de las tolerancias que fijes. Las discrepancias van a la cola de excepciones, no a tu bandeja de entrada.',
	'marketing.features.erp.title': 'Sincronización con el ERP en ambos sentidos',
	'marketing.features.erp.body':
		'Las facturas aprobadas se contabilizan en NetSuite o Dynamics 365 Business Central, o en otros sistemas contables a través de Merge.dev. El estado del pago vuelve, así que tu ERP sigue siendo el sistema de referencia.',
	'marketing.features.card.title': 'Pagos con tarjeta virtual',
	'marketing.features.card.body':
		'Paga cada factura con su propia tarjeta virtual de un solo uso, emitida a través de Lithic o Nium, y obtén un reembolso típico del 1–2 % en los pagos a proveedores que aceptan tarjeta.',
	'marketing.features.exceptions.title': 'Cola de excepciones',
	'marketing.features.exceptions.body':
		'Duplicados, alertas de fraude, importes por encima del límite y proveedores sin verificar se marcan, se asignan y se resuelven en una sola cola, sin rebuscar en el correo.',

	'marketing.how.eyebrow': 'Cómo funciona',
	'marketing.how.heading': 'De la factura al libro mayor, en tres pasos.',
	'marketing.how.capture.title': 'Captura',
	'marketing.how.capture.body':
		'Reenvía las facturas por correo, súbelas o arrástralas, o hazles una foto con la app móvil. Funciona con PDF, imágenes y papel escaneado.',
	'marketing.how.route.title': 'Lectura y enrutado',
	'marketing.how.route.body':
		'La IA identifica el proveedor, el importe, la cuenta contable y las líneas. Tu flujo de trabajo envía la factura al aprobador adecuado de forma automática.',
	'marketing.how.pay.title': 'Pago y sincronización',
	'marketing.how.pay.body':
		'Las facturas aprobadas se contabilizan en tu ERP y se programan para pago por ACH, transferencia, cheque o tarjeta virtual.',

	'marketing.tally.eyebrow': 'Por qué cambian los equipos',
	'marketing.tally.heading': 'Hecho para finanzas, no solo vendido a finanzas.',
	'marketing.tally.lede':
		'La Hacienda inglesa saldaba una deuda con una tarja: un palo con muescas, partido a lo largo, una mitad para cada parte; la deuda quedaba pagada cuando las dos mitades coincidían. Ese sigue siendo el trabajo: la factura frente a la orden de compra, el pago frente al extracto. Todo aquí existe para que las dos mitades coincidan.',
	'marketing.tally.alt':
		'Una tarja dorada con muescas, partida a lo largo en dos mitades cuyas muescas coinciden entre sí.',
	'marketing.diff.ai.title': 'IA bajo tu control',
	'marketing.diff.ai.body':
		'Usa nuestra IA integrada o conecta tu propia cuenta de Claude, OpenAI o AWS Textract, con tus propias condiciones de conservación de datos. Si las facturas nunca deben salir de tus servidores, ejecuta allí la IA con Ollama.',
	'marketing.diff.connect.title': 'Funciona con lo que ya tienes',
	'marketing.diff.connect.body':
		'Tu ERP, tu banco, tu emisor de tarjetas y tu correo siguen donde están: FeohLedger se conecta a cada uno. Si más adelante cambias de proveedor, tus reglas de aprobación y tu historial se mantienen.',
	'marketing.diff.cards.title': 'Tarjetas que te devuelven dinero',
	'marketing.diff.cards.body':
		'Los pagos con tarjeta virtual a través de Lithic o Nium generan un reembolso típico del 1–2 %. Si {monthly} al mes en facturas pasaran a pagarse con tarjeta, volverían a tu presupuesto entre {low} y {high} al año.',
	'marketing.diff.cards.note':
		'Ejemplo ilustrativo. El reembolso depende de la tarifa que negocies, de cuánto gasto pase a tarjeta y de qué proveedores la acepten.',
	'marketing.diff.mobile.title': 'Móvil sin concesiones',
	'marketing.diff.mobile.body':
		'Apps nativas para iOS y Android con captura por cámara, acceso biométrico, aprobación con un deslizamiento y modo sin conexión. Los aprobadores pueden despejar la cola desde la sala de espera del aeropuerto.',

	'marketing.final.heading': 'Tu espacio de trabajo, listo en minutos.',
	'marketing.final.body':
		'Elige la dirección de tu espacio de trabajo, confirma tu correo y define tu contraseña desde el enlace que te enviamos. El plan Free no pide tarjeta, ni contrato, ni llamada comercial.',

	'marketing.footer.label': 'Pie de página',
	'marketing.footer.copyright': '© {year} {product}.',

	'marketing.findWorkspace.pageTitle': 'Iniciar sesión — {product}',
	'marketing.findWorkspace.heading': 'Inicia sesión en tu espacio de trabajo',
	'marketing.findWorkspace.subtitle':
		'Cada empresa inicia sesión en la dirección de su propio espacio de trabajo. Escribe la tuya y te llevamos a su página de inicio de sesión.',
	'marketing.findWorkspace.label': 'Dirección del espacio de trabajo',
	'marketing.findWorkspace.hint':
		'Está en tu correo de bienvenida y en el enlace de cualquier correo que te haya enviado tu espacio de trabajo.',
	'marketing.findWorkspace.submit': 'Continuar',
	'marketing.findWorkspace.going': 'Abriendo {host}…',
	'marketing.findWorkspace.problem.empty': 'Escribe la dirección de tu espacio de trabajo.',
	'marketing.findWorkspace.problem.startLetter': 'La dirección de un espacio de trabajo empieza por una letra.',
	'marketing.findWorkspace.problem.characters':
		'Escribe solo la primera parte de la dirección: minúsculas, números y guiones.',
	'marketing.findWorkspace.problem.tooShort': 'La dirección de un espacio de trabajo tiene al menos 3 caracteres.',
	'marketing.findWorkspace.problem.tooLong': 'La dirección de un espacio de trabajo tiene como máximo 30 caracteres.',
	'marketing.findWorkspace.problem.endHyphen': 'La dirección de un espacio de trabajo no puede terminar en guion.',
	'marketing.findWorkspace.problem.doubleHyphen':
		'La dirección de un espacio de trabajo no puede tener dos guiones seguidos.',
	'marketing.findWorkspace.ownDomain': 'Parece la dirección propia de tu empresa. {link}',
	'marketing.findWorkspace.ownDomainLink': 'Iniciar sesión en {host}',
	'marketing.findWorkspace.configError':
		'No pudimos cargar los datos de inicio de sesión. Comprueba tu conexión e inténtalo de nuevo.',
	'marketing.findWorkspace.retry': 'Reintentar',
	'marketing.findWorkspace.lost':
		'¿No la encuentras? Pregunta a alguien de tu equipo que ya inicie sesión: la dirección aparece en la barra de direcciones de su navegador.',
	'marketing.findWorkspace.noWorkspace': '¿Nuevo en {product}? {create}',
	'marketing.findWorkspace.panelHeading': 'Por qué te pedimos la dirección',
	'marketing.findWorkspace.panelBody':
		'Cada empresa tiene su propio espacio de trabajo, con su propio inicio de sesión. Nunca buscamos un espacio de trabajo a partir de un correo, así que nadie puede usar esta página para averiguar dónde trabaja alguien.'
} satisfies MarketingMessages;
