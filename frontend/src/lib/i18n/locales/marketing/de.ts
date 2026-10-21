import type { MarketingMessages } from '../../messages';

// German marketing slice: the apex landing page and "find your workspace".
// Lazy-loaded by `ensureSlices('marketing')` in store.svelte.ts; `satisfies
// MarketingMessages` makes a missing or extra key a compile error. Provider
// names are proper nouns and stay as written. Every figure is substantiated in
// docs/marketing-substantiation.md.
export const messages = {
	'marketing.nav.label': 'Hauptnavigation',
	'marketing.nav.features': 'Funktionen',
	'marketing.nav.how': 'So funktioniert’s',
	'marketing.nav.pricing': 'Preise',
	'marketing.nav.signIn': 'Anmelden',
	'marketing.nav.menu': 'Menü',
	'marketing.cta.create': 'Arbeitsbereich erstellen',
	'marketing.cta.short': 'Gratis starten',

	'marketing.hero.eyebrow': 'Kreditorenbuchhaltung, automatisiert',
	'marketing.hero.title': 'AP-Automatisierung, die Ihr Finanzteam',
	'marketing.hero.titleAccent': 'wirklich gern nutzt.',
	'marketing.hero.lede':
		'Von der Rechnung zur Zahlung in Minuten statt Tagen. KI liest Ihre Rechnungen, Freigaben folgen Ihren Regeln, und freigegebene Rechnungen werden in Ihr ERP gebucht – mit typischerweise 1–2 % Rückvergütung auf Zahlungen an Lieferanten, die Karten akzeptieren.',
	'marketing.hero.secondary': 'So funktioniert’s',
	'marketing.hero.subNote': 'Kostenloser Einstieg · Keine Kreditkarte · Selbst registrieren',

	'marketing.demo.status': 'Zur Prüfung bereit',
	'marketing.demo.matched': '3-Wege-Abgleich',
	'marketing.demo.vendor': 'Lieferant',
	'marketing.demo.invoiceNo': 'Rechnungsnr.',
	'marketing.demo.amount': 'Betrag',
	'marketing.demo.due': 'Fällig',
	'marketing.demo.glCode': 'Sachkonto',
	'marketing.demo.glName': 'Wareneinsatz',
	'marketing.demo.stamp': 'Freigegeben · Per virtueller Karte bezahlt',
	'marketing.demo.stage.capture': 'Erfassen',
	'marketing.demo.stage.extract': 'Auslesen',
	'marketing.demo.stage.approve': 'Freigeben',
	'marketing.demo.stage.pay': 'Bezahlen',

	'marketing.adapters.heading': 'Integrierte Anbindungen',

	'marketing.stats.rails': 'Zahlungswege, von ACH bis CHAPS',
	'marketing.stats.stepTypes': 'Arten von Workflow-Schritten',
	'marketing.stats.languages': 'Sprachen der Oberfläche',
	'marketing.stats.rebateValue': '1–2 %',
	'marketing.stats.rebate': 'typische Rückvergütung auf Kartenzahlungen',

	'marketing.features.eyebrow': 'Alles, was Sie brauchen',
	'marketing.features.heading': 'Eine Plattform, von der Erfassung bis zur Zahlung.',
	'marketing.features.lede':
		'Die meisten Teams setzen Rechnungserfassung, Freigaben, Abgleich und ERP-Buchung aus vier verschiedenen Tools zusammen. FeohLedger erledigt alles an einem Ort und arbeitet mit den Systemen, die Sie bereits nutzen.',
	'marketing.features.extract.title': 'Rechnungen per KI auslesen',
	'marketing.features.extract.body':
		'PDF oder Foto hochladen – die KI liest jedes Feld und jede Position und zeigt, wie sicher sie sich jeweils ist. So prüft Ihr Team nur, was wirklich geprüft werden muss.',
	'marketing.features.approve.title': 'Freigabe-Workflows',
	'marketing.features.approve.body':
		'Leiten Sie Rechnungen nach Betrag, Lieferant oder Sachkonto weiter – ganz ohne Programmierung. Funktionstrennung und ein lückenloser Prüfpfad sind eingebaut, und Freigaben erteilen Sie per Slack, Microsoft Teams, E-Mail oder mobiler App.',
	'marketing.features.match.title': '2- und 3-Wege-Bestellabgleich',
	'marketing.features.match.body':
		'Rechnungen werden innerhalb Ihrer Toleranzen mit Bestellungen und Wareneingängen abgeglichen. Abweichungen landen in der Ausnahmenliste, nicht in Ihrem Postfach.',
	'marketing.features.erp.title': 'ERP-Abgleich in beide Richtungen',
	'marketing.features.erp.body':
		'Freigegebene Rechnungen werden in NetSuite oder Dynamics 365 Business Central gebucht, oder über Merge.dev in weitere Buchhaltungssysteme. Der Zahlungsstatus kommt zurück, sodass Ihr ERP das führende System bleibt.',
	'marketing.features.card.title': 'Zahlung per virtueller Karte',
	'marketing.features.card.body':
		'Bezahlen Sie jede Rechnung mit einer eigenen virtuellen Einmalkarte, ausgegeben über Lithic oder Nium, und erhalten Sie typischerweise 1–2 % Rückvergütung auf Zahlungen an Lieferanten, die Karten akzeptieren.',
	'marketing.features.exceptions.title': 'Ausnahmenliste',
	'marketing.features.exceptions.body':
		'Dubletten, Betrugsverdacht, Betragsüberschreitungen und unbestätigte Lieferanten werden markiert, zugewiesen und in einer Liste geklärt – ohne Suche im E-Mail-Postfach.',

	'marketing.how.eyebrow': 'So funktioniert’s',
	'marketing.how.heading': 'Von der Rechnung ins Hauptbuch, in drei Schritten.',
	'marketing.how.capture.title': 'Erfassen',
	'marketing.how.capture.body':
		'Rechnungen per E-Mail weiterleiten, hochladen, hineinziehen oder in der mobilen App fotografieren. PDFs, Bilder und eingescannte Papierbelege funktionieren alle.',
	'marketing.how.route.title': 'Auslesen und weiterleiten',
	'marketing.how.route.body':
		'Die KI erkennt Lieferant, Betrag, Sachkonto und Positionen. Ihr Workflow leitet die Rechnung automatisch an die richtige Person zur Freigabe.',
	'marketing.how.pay.title': 'Bezahlen und abgleichen',
	'marketing.how.pay.body':
		'Freigegebene Rechnungen werden in Ihr ERP gebucht und zur Zahlung per ACH, Überweisung, Scheck oder virtueller Karte eingeplant.',

	'marketing.tally.eyebrow': 'Warum Teams wechseln',
	'marketing.tally.heading': 'Für die Finanzabteilung gebaut, nicht nur an sie vermarktet.',
	'marketing.tally.lede':
		'Das englische Schatzamt hielt Schulden auf einem Kerbholz fest: einkerben, der Länge nach spalten, jeder Partei eine Hälfte geben – beglichen war die Schuld, wenn beide Hälften zusammenpassten. Genau das ist bis heute die Aufgabe: die Rechnung gegen die Bestellung, die Zahlung gegen den Kontoauszug. Alles hier dient dazu, dass die Hälften zusammenpassen.',
	'marketing.tally.alt':
		'Ein gekerbtes goldenes Kerbholz, der Länge nach in zwei Hälften gespalten; die Kerben beider Hälften liegen genau übereinander.',
	'marketing.diff.ai.title': 'KI unter Ihrer Kontrolle',
	'marketing.diff.ai.body':
		'Nutzen Sie unsere eingebaute KI oder verbinden Sie Ihr eigenes Konto bei Claude, OpenAI oder AWS Textract – zu Ihren eigenen Aufbewahrungsbedingungen. Dürfen Rechnungen Ihre Server nie verlassen, läuft die KI mit Ollama direkt dort.',
	'marketing.diff.connect.title': 'Passt zu Ihren bestehenden Systemen',
	'marketing.diff.connect.body':
		'ERP, Bank, Kartenanbieter und E-Mail bleiben, wo sie sind – FeohLedger verbindet sich mit jedem davon. Wechseln Sie später einen dieser Anbieter, bleiben Ihre Freigaberegeln und Ihre Historie erhalten.',
	'marketing.diff.cards.title': 'Karten, die sich auszahlen',
	'marketing.diff.cards.body':
		'Zahlungen per virtueller Karte über Lithic oder Nium bringen typischerweise 1–2 % Rückvergütung. Würden monatlich Rechnungen über {monthly} per Karte bezahlt, flössen so {low}–{high} im Jahr zurück in Ihr Budget.',
	'marketing.diff.cards.note':
		'Rechenbeispiel. Die Rückvergütung hängt von Ihrem ausgehandelten Satz ab, davon, wie viel Volumen auf Karte umgestellt wird, und davon, welche Lieferanten Karten akzeptieren.',
	'marketing.diff.mobile.title': 'Mobil ohne Abstriche',
	'marketing.diff.mobile.body':
		'Native Apps für iOS und Android mit Kameraerfassung, biometrischer Anmeldung, Freigabe per Wischgeste und Offline-Modus. Freigaben erteilen Sie so auch aus der Flughafen-Lounge.',

	'marketing.final.heading': 'Ihr Arbeitsbereich steht in wenigen Minuten.',
	'marketing.final.body':
		'Wählen Sie die Adresse Ihres Arbeitsbereichs, bestätigen Sie Ihre E-Mail-Adresse und legen Sie über den Link, den wir Ihnen senden, Ihr Passwort fest. Der Free-Tarif kommt ohne Kreditkarte, ohne Vertrag und ohne Verkaufsgespräch aus.',

	'marketing.footer.label': 'Fußzeile',
	'marketing.footer.copyright': '© {year} {product}.',

	'marketing.findWorkspace.pageTitle': 'Anmelden – {product}',
	'marketing.findWorkspace.heading': 'Bei Ihrem Arbeitsbereich anmelden',
	'marketing.findWorkspace.subtitle':
		'Jedes Unternehmen meldet sich unter der Adresse seines eigenen Arbeitsbereichs an. Geben Sie Ihre ein, und wir bringen Sie zur passenden Anmeldeseite.',
	'marketing.findWorkspace.label': 'Adresse des Arbeitsbereichs',
	'marketing.findWorkspace.hint':
		'Sie steht in Ihrer Willkommens-E-Mail und im Link jeder E-Mail, die Ihr Arbeitsbereich Ihnen geschickt hat.',
	'marketing.findWorkspace.submit': 'Weiter',
	'marketing.findWorkspace.going': '{host} wird geöffnet …',
	'marketing.findWorkspace.problem.empty': 'Geben Sie die Adresse Ihres Arbeitsbereichs ein.',
	'marketing.findWorkspace.problem.startLetter': 'Die Adresse eines Arbeitsbereichs beginnt mit einem Buchstaben.',
	'marketing.findWorkspace.problem.characters':
		'Geben Sie nur den ersten Teil der Adresse ein – Kleinbuchstaben, Ziffern und Bindestriche.',
	'marketing.findWorkspace.problem.tooShort': 'Die Adresse eines Arbeitsbereichs hat mindestens 3 Zeichen.',
	'marketing.findWorkspace.problem.tooLong': 'Die Adresse eines Arbeitsbereichs hat höchstens 30 Zeichen.',
	'marketing.findWorkspace.problem.endHyphen': 'Die Adresse eines Arbeitsbereichs darf nicht mit einem Bindestrich enden.',
	'marketing.findWorkspace.problem.doubleHyphen':
		'Die Adresse eines Arbeitsbereichs darf keine zwei Bindestriche hintereinander enthalten.',
	'marketing.findWorkspace.ownDomain': 'Das sieht nach der eigenen Adresse Ihres Unternehmens aus. {link}',
	'marketing.findWorkspace.ownDomainLink': 'Bei {host} anmelden',
	'marketing.findWorkspace.configError':
		'Die Anmeldedaten konnten nicht geladen werden. Prüfen Sie Ihre Verbindung und versuchen Sie es erneut.',
	'marketing.findWorkspace.retry': 'Erneut versuchen',
	'marketing.findWorkspace.lost':
		'Nicht zur Hand? Fragen Sie im Team nach – wer sich schon anmeldet, sieht die Adresse in der Adresszeile des Browsers.',
	'marketing.findWorkspace.noWorkspace': 'Neu bei {product}? {create}',
	'marketing.findWorkspace.panelHeading': 'Warum wir nach der Adresse fragen',
	'marketing.findWorkspace.panelBody':
		'Jedes Unternehmen hat einen eigenen Arbeitsbereich mit eigener Anmeldung. Wir suchen einen Arbeitsbereich nie anhand einer E-Mail-Adresse heraus – so kann niemand über diese Seite herausfinden, wo jemand arbeitet.'
} satisfies MarketingMessages;
