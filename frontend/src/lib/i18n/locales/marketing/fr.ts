import type { MarketingMessages } from '../../messages';

// French marketing slice: the apex landing page and "find your workspace".
// Lazy-loaded by `ensureSlices('marketing')` in store.svelte.ts; `satisfies
// MarketingMessages` makes a missing or extra key a compile error. Provider
// names are proper nouns and stay as written. Every figure is substantiated in
// docs/marketing-substantiation.md.
export const messages = {
	'marketing.nav.label': 'Principale',
	'marketing.nav.features': 'Fonctionnalités',
	'marketing.nav.how': 'Fonctionnement',
	'marketing.nav.pricing': 'Tarifs',
	'marketing.nav.signIn': 'Se connecter',
	'marketing.nav.menu': 'Menu',
	'marketing.cta.create': 'Créer un espace de travail',
	'marketing.cta.short': 'C’est gratuit',

	'marketing.hero.eyebrow': 'La comptabilité fournisseurs, automatisée',
	'marketing.hero.title': 'L’automatisation des factures fournisseurs',
	'marketing.hero.titleAccent': 'que votre équipe finance adoptera.',
	'marketing.hero.lede':
		'De la facture au paiement en quelques minutes, pas en quelques jours. L’IA lit vos factures, les validations suivent vos règles et les factures approuvées sont comptabilisées dans votre ERP — avec une remise typique de 1–2 % sur les paiements aux fournisseurs qui acceptent la carte.',
	'marketing.hero.secondary': 'Voir le fonctionnement',
	'marketing.hero.subNote': 'Gratuit pour commencer · Sans carte bancaire · Inscription en libre-service',

	'marketing.demo.status': 'Prête pour revue',
	'marketing.demo.matched': 'Rapprochement à 3 voies',
	'marketing.demo.vendor': 'Fournisseur',
	'marketing.demo.invoiceNo': 'N° de facture',
	'marketing.demo.amount': 'Montant',
	'marketing.demo.due': 'Échéance',
	'marketing.demo.glCode': 'Compte',
	'marketing.demo.glName': 'Coût des ventes',
	'marketing.demo.stamp': 'Approuvée · Payée par carte virtuelle',
	'marketing.demo.stage.capture': 'Saisie',
	'marketing.demo.stage.extract': 'Lecture',
	'marketing.demo.stage.approve': 'Validation',
	'marketing.demo.stage.pay': 'Paiement',

	'marketing.adapters.heading': 'Intégrations incluses',

	'marketing.stats.rails': 'moyens de paiement, de l’ACH au CHAPS',
	'marketing.stats.stepTypes': 'types d’étapes de workflow',
	'marketing.stats.languages': 'langues d’interface',
	'marketing.stats.rebateValue': '1–2 %',
	'marketing.stats.rebate': 'de remise typique quand le fournisseur accepte la carte',

	'marketing.features.eyebrow': 'Tout ce qu’il vous faut',
	'marketing.features.heading': 'Une seule plateforme, de la saisie au paiement.',
	'marketing.features.lede':
		'La plupart des équipes jonglent entre quatre outils pour la saisie des factures, les validations, le rapprochement et la comptabilisation dans l’ERP. FeohLedger fait tout au même endroit, avec les systèmes que vous utilisez déjà.',
	'marketing.features.extract.title': 'Lecture des factures par IA',
	'marketing.features.extract.body':
		'Déposez un PDF ou une photo. L’IA lit chaque champ et chaque ligne, et indique son degré de certitude pour chacun : votre équipe ne vérifie que ce qui doit l’être.',
	'marketing.features.approve.title': 'Circuits de validation',
	'marketing.features.approve.body':
		'Orientez les factures selon le montant, le fournisseur ou le compte comptable, sans écrire une ligne de code. Séparation des tâches et piste d’audit complète sont intégrées, et les valideurs approuvent depuis Slack, Microsoft Teams, l’e-mail ou l’application mobile.',
	'marketing.features.match.title': 'Rapprochement à 2 et 3 voies',
	'marketing.features.match.body':
		'Les factures sont rapprochées des bons de commande et des réceptions, selon les tolérances que vous fixez. Les écarts arrivent dans la file des exceptions, pas dans votre boîte mail.',
	'marketing.features.erp.title': 'Synchronisation ERP dans les deux sens',
	'marketing.features.erp.body':
		'Les factures approuvées sont comptabilisées dans NetSuite ou Dynamics 365 Business Central, ou dans d’autres logiciels comptables via Merge.dev. Le statut de paiement remonte, et votre ERP reste la référence.',
	'marketing.features.card.title': 'Paiement par carte virtuelle',
	'marketing.features.card.body':
		'Réglez chaque facture avec sa propre carte virtuelle à usage unique, émise via Lithic ou Nium, et obtenez une remise typique de 1–2 % sur les paiements aux fournisseurs qui acceptent la carte.',
	'marketing.features.exceptions.title': 'File des exceptions',
	'marketing.features.exceptions.body':
		'Doublons, soupçons de fraude, dépassements de montant et fournisseurs non vérifiés sont signalés, attribués et traités dans une seule file — sans fouiller vos e-mails.',

	'marketing.how.eyebrow': 'Fonctionnement',
	'marketing.how.heading': 'De la facture au grand livre, en trois étapes.',
	'marketing.how.capture.title': 'Saisir',
	'marketing.how.capture.body':
		'Transférez vos factures par e-mail, importez-les, glissez-les ou prenez-les en photo dans l’application mobile. PDF, images et documents papier numérisés sont acceptés.',
	'marketing.how.route.title': 'Lire et orienter',
	'marketing.how.route.body':
		'L’IA repère le fournisseur, le montant, le compte et les lignes. Votre circuit envoie la facture au bon valideur, automatiquement.',
	'marketing.how.pay.title': 'Payer et synchroniser',
	'marketing.how.pay.body':
		'Les factures approuvées sont comptabilisées dans votre ERP et programmées au paiement par ACH, virement, chèque ou carte virtuelle.',

	'marketing.tally.eyebrow': 'Pourquoi les équipes changent',
	'marketing.tally.heading': 'Conçu pour la finance, pas seulement vendu à la finance.',
	'marketing.tally.lede':
		'L’Échiquier anglais réglait une dette avec une taille : un bâton entaillé, fendu en deux, une moitié pour chaque partie — la dette était soldée quand les deux moitiés concordaient. C’est toujours le même travail : la facture face au bon de commande, le paiement face au relevé. Tout ici existe pour que les deux moitiés concordent.',
	'marketing.tally.alt':
		'Un bâton de taille doré et entaillé, fendu dans sa longueur en deux moitiés dont les entailles se correspondent.',
	'marketing.diff.ai.title': 'Une IA que vous maîtrisez',
	'marketing.diff.ai.body':
		'Utilisez notre IA intégrée, ou connectez votre propre compte Claude, OpenAI ou AWS Textract, selon vos propres règles de conservation des données. Si vos factures ne doivent jamais quitter vos serveurs, faites-y tourner l’IA avec Ollama.',
	'marketing.diff.connect.title': 'S’intègre à vos systèmes actuels',
	'marketing.diff.connect.body':
		'Votre ERP, votre banque, votre émetteur de cartes et votre messagerie restent en place : FeohLedger se connecte à chacun d’eux. Si vous changez de prestataire plus tard, vos règles de validation et votre historique restent intacts.',
	'marketing.diff.cards.title': 'Des cartes qui vous rapportent',
	'marketing.diff.cards.body':
		'Les paiements par carte virtuelle via Lithic ou Nium rapportent une remise typique de 1–2 %. Si {monthly} de factures par mois passaient par carte, cela ferait revenir {low} à {high} par an dans votre budget.',
	'marketing.diff.cards.note':
		'Exemple indicatif. La remise dépend du taux négocié, de la part des dépenses réglée par carte et des fournisseurs qui l’acceptent.',
	'marketing.diff.mobile.title': 'Le mobile sans compromis',
	'marketing.diff.mobile.body':
		'Applications natives iOS et Android avec capture par l’appareil photo, connexion biométrique, validation d’un geste et mode hors ligne. Vos valideurs débloquent la comptabilité fournisseurs depuis un salon d’aéroport.',

	'marketing.final.heading': 'Votre espace de travail, prêt en quelques minutes.',
	'marketing.final.body':
		'Choisissez l’adresse de votre espace de travail, confirmez votre e-mail, puis définissez votre mot de passe grâce au lien que nous vous envoyons. L’offre Free se passe de carte bancaire, de contrat et de rendez-vous commercial.',

	'marketing.footer.label': 'Pied de page',
	'marketing.footer.copyright': '© {year} {product}.',

	'marketing.findWorkspace.pageTitle': 'Connexion — {product}',
	'marketing.findWorkspace.heading': 'Connectez-vous à votre espace de travail',
	'marketing.findWorkspace.subtitle':
		'Chaque entreprise se connecte à l’adresse de son propre espace de travail. Saisissez la vôtre et nous vous conduirons à sa page de connexion.',
	'marketing.findWorkspace.label': 'Adresse de l’espace de travail',
	'marketing.findWorkspace.hint':
		'Elle figure dans votre e-mail de bienvenue, et dans le lien de chaque e-mail envoyé par votre espace de travail.',
	'marketing.findWorkspace.submit': 'Continuer',
	'marketing.findWorkspace.going': 'Ouverture de {host}…',
	'marketing.findWorkspace.problem.empty': 'Saisissez l’adresse de votre espace de travail.',
	'marketing.findWorkspace.problem.startLetter': 'L’adresse d’un espace de travail commence par une lettre.',
	'marketing.findWorkspace.problem.characters':
		'Saisissez seulement la première partie de l’adresse : lettres minuscules, chiffres et traits d’union.',
	'marketing.findWorkspace.problem.tooShort': 'L’adresse d’un espace de travail compte au moins 3 caractères.',
	'marketing.findWorkspace.problem.tooLong': 'L’adresse d’un espace de travail compte au plus 30 caractères.',
	'marketing.findWorkspace.problem.endHyphen':
		'L’adresse d’un espace de travail ne peut pas se terminer par un trait d’union.',
	'marketing.findWorkspace.problem.doubleHyphen':
		'L’adresse d’un espace de travail ne peut pas contenir deux traits d’union à la suite.',
	'marketing.findWorkspace.ownDomain': 'Cela ressemble à l’adresse propre de votre entreprise. {link}',
	'marketing.findWorkspace.ownDomainLink': 'Se connecter sur {host}',
	'marketing.findWorkspace.configError':
		'Impossible de charger les informations de connexion. Vérifiez votre connexion et réessayez.',
	'marketing.findWorkspace.retry': 'Réessayer',
	'marketing.findWorkspace.lost':
		'Vous ne la trouvez pas ? Demandez à un collègue qui se connecte déjà : l’adresse figure dans la barre d’adresse de son navigateur.',
	'marketing.findWorkspace.noWorkspace': 'Nouveau sur {product} ? {create}',
	'marketing.findWorkspace.panelHeading': 'Pourquoi nous demandons l’adresse',
	'marketing.findWorkspace.panelBody':
		'Chaque entreprise dispose de son propre espace de travail, avec sa propre connexion. Nous ne recherchons jamais un espace de travail à partir d’une adresse e-mail : personne ne peut donc se servir de cette page pour savoir où travaille quelqu’un.'
} satisfies MarketingMessages;
