import type { MarketingMessages } from '../../messages';

// Japanese marketing slice: the apex landing page and "find your workspace".
// Lazy-loaded by `ensureSlices('marketing')` in store.svelte.ts; `satisfies
// MarketingMessages` makes a missing or extra key a compile error. Provider
// names are proper nouns and stay as written. Every figure is substantiated in
// docs/marketing-substantiation.md.
export const messages = {
	'marketing.nav.label': 'メイン',
	'marketing.nav.features': '機能',
	'marketing.nav.how': '使い方',
	'marketing.nav.pricing': '料金',
	'marketing.nav.signIn': 'サインイン',
	'marketing.nav.menu': 'メニュー',
	'marketing.cta.create': 'ワークスペースを作成',
	'marketing.cta.short': '無料で始める',

	'marketing.hero.eyebrow': '買掛金業務を自動化',
	'marketing.hero.title': '経理チームが',
	'marketing.hero.titleAccent': '本当に使いたくなる買掛金自動化。',
	'marketing.hero.lede':
		'請求書の受領から支払いまで、数日ではなく数分で。AI が請求書を読み取り、承認はあなたのルールどおりに進み、承認済みの請求書は ERP に計上されます。カード払いに対応する仕入先への支払いでは、一般に 1〜2% のリベートも得られます。',
	'marketing.hero.secondary': '使い方を見る',
	'marketing.hero.subNote': '無料で開始 · クレジットカード不要 · セルフサービスで登録',

	'marketing.demo.status': 'レビュー待ち',
	'marketing.demo.matched': '3 点照合済み',
	'marketing.demo.vendor': '仕入先',
	'marketing.demo.invoiceNo': '請求書番号',
	'marketing.demo.amount': '金額',
	'marketing.demo.due': '支払期日',
	'marketing.demo.glCode': '勘定科目',
	'marketing.demo.glName': '売上原価',
	'marketing.demo.stamp': '承認済み · バーチャルカードで支払済み',
	'marketing.demo.stage.capture': '取り込み',
	'marketing.demo.stage.extract': '読み取り',
	'marketing.demo.stage.approve': '承認',
	'marketing.demo.stage.pay': '支払い',

	'marketing.adapters.heading': '標準搭載の連携先',

	'marketing.stats.rails': '支払い手段（ACH から CHAPS まで）',
	'marketing.stats.stepTypes': 'ワークフローのステップ種別',
	'marketing.stats.languages': '画面の対応言語',
	'marketing.stats.rebateValue': '1〜2%',
	'marketing.stats.rebate': 'カード払い対応の支払いでの一般的なリベート',

	'marketing.features.eyebrow': '必要なものはすべて',
	'marketing.features.heading': '受領から支払いまで、ひとつのプラットフォームで。',
	'marketing.features.lede':
		'多くのチームは、請求書の取り込み、承認、照合、ERP への計上を 4 つのツールでつないでいます。FeohLedger はそのすべてを 1 か所で行い、今お使いのシステムとそのまま連携します。',
	'marketing.features.extract.title': 'AI による請求書の読み取り',
	'marketing.features.extract.body':
		'PDF や写真を入れるだけ。AI がすべての項目と明細を読み取り、それぞれの確からしさを示すので、チームは確認が必要な箇所だけを確認できます。',
	'marketing.features.approve.title': '承認ワークフロー',
	'marketing.features.approve.body':
		'金額・仕入先・勘定科目ごとに、コードを書かずに承認経路を設定できます。職務分掌と完全な監査証跡を標準で備え、承認は Slack、Microsoft Teams、メール、モバイルアプリから行えます。',
	'marketing.features.match.title': '発注書との 2 点・3 点照合',
	'marketing.features.match.body':
		'請求書を発注書や入荷記録と、設定した許容範囲で照合します。不一致は受信トレイではなく例外キューに入ります。',
	'marketing.features.erp.title': 'ERP との双方向同期',
	'marketing.features.erp.body':
		'承認済みの請求書を NetSuite や Dynamics 365 Business Central、または Merge.dev 経由でその他の会計システムに計上します。支払ステータスも戻るため、ERP が常に正となる記録であり続けます。',
	'marketing.features.card.title': 'バーチャルカードでの支払い',
	'marketing.features.card.body':
		'請求書ごとに使い捨てのバーチャルカードを Lithic または Nium で発行して支払い、カード払いに対応する仕入先への支払いで一般に 1〜2% のリベートを得られます。',
	'marketing.features.exceptions.title': '例外キュー',
	'marketing.features.exceptions.body':
		'重複、不正の疑い、限度額超過、未確認の仕入先を検出し、担当者に割り当て、ひとつのキューで解決します。メールを探し回る必要はありません。',

	'marketing.how.eyebrow': '使い方',
	'marketing.how.heading': '請求書から元帳まで、3 つのステップ。',
	'marketing.how.capture.title': '取り込む',
	'marketing.how.capture.body':
		'請求書をメールで転送、アップロード、ドラッグ＆ドロップするか、モバイルアプリで撮影します。PDF、画像、スキャンした紙の請求書に対応します。',
	'marketing.how.route.title': '読み取って回す',
	'marketing.how.route.body':
		'AI が仕入先、金額、勘定科目、明細を読み取り、ワークフローが適切な承認者へ自動で回します。',
	'marketing.how.pay.title': '支払って同期する',
	'marketing.how.pay.body':
		'承認済みの請求書は ERP に計上され、ACH、銀行振込、小切手、バーチャルカードでの支払いが予定されます。',

	'marketing.tally.eyebrow': '乗り換えの理由',
	'marketing.tally.heading': '経理に売り込むためではなく、経理のために作りました。',
	'marketing.tally.lede':
		'かつて英国の財務府は、刻み目を入れた木の棒を縦に割り、半分ずつを双方に渡して債務を記録しました。2 つの半分が合ったとき、債務は清算されたのです。仕事の本質は今も同じです。請求書と発注書、支払いと取引明細。ここにあるものはすべて、その 2 つの半分を合わせるためにあります。',
	'marketing.tally.alt':
		'刻み目の入った金色の割符（タリースティック）。縦に 2 つに割られ、両方の刻み目がぴたりと揃っている。',
	'marketing.diff.ai.title': '自社で管理できる AI',
	'marketing.diff.ai.body':
		'内蔵の AI を使うことも、自社の Claude、OpenAI、AWS Textract のアカウントを自社のデータ保持条件のもとで接続することもできます。請求書を社外に出せない場合は、Ollama で自社サーバー上で AI を動かせます。',
	'marketing.diff.connect.title': '今のシステムをそのまま活かせる',
	'marketing.diff.connect.body':
		'ERP、銀行、カード発行会社、メールはそのまま。FeohLedger がそれぞれに接続します。あとでいずれかの提供元を替えても、承認ルールと履歴はそのまま残ります。',
	'marketing.diff.cards.title': '還元のあるカード払い',
	'marketing.diff.cards.body':
		'Lithic または Nium によるバーチャルカード払いでは、一般に 1〜2% のリベートが得られます。毎月 {monthly} の請求書をカード払いに移せば、年間 {low}〜{high} が予算に戻ってくる計算です。',
	'marketing.diff.cards.note':
		'試算例です。リベートは交渉した料率、カード払いに移す支出の割合、カード払いに対応する仕入先によって変わります。',
	'marketing.diff.mobile.title': '妥協のないモバイル',
	'marketing.diff.mobile.body':
		'iOS と Android のネイティブアプリで、カメラでの取り込み、生体認証によるサインイン、スワイプでの承認、オフラインモードに対応。承認者は空港のラウンジからでも承認待ちを片付けられます。',

	'marketing.final.heading': 'ワークスペースは数分で用意できます。',
	'marketing.final.body':
		'ワークスペースのアドレスを決め、メールアドレスを確認し、お送りするリンクからパスワードを設定するだけ。Free プランならカードも契約も営業との打ち合わせも不要です。',

	'marketing.footer.label': 'フッター',
	'marketing.footer.copyright': '© {year} {product}.',

	'marketing.findWorkspace.pageTitle': 'サインイン — {product}',
	'marketing.findWorkspace.heading': 'ワークスペースにサインイン',
	'marketing.findWorkspace.subtitle':
		'各社はそれぞれのワークスペースのアドレスでサインインします。アドレスを入力すると、そのサインインページにご案内します。',
	'marketing.findWorkspace.label': 'ワークスペースのアドレス',
	'marketing.findWorkspace.hint':
		'ウェルカムメールや、ワークスペースから届いたメールのリンクに記載されています。',
	'marketing.findWorkspace.submit': '続ける',
	'marketing.findWorkspace.going': '{host} を開いています…',
	'marketing.findWorkspace.problem.empty': 'ワークスペースのアドレスを入力してください。',
	'marketing.findWorkspace.problem.startLetter': 'ワークスペースのアドレスは英字で始まります。',
	'marketing.findWorkspace.problem.characters':
		'アドレスの最初の部分だけを、英小文字・数字・ハイフンで入力してください。',
	'marketing.findWorkspace.problem.tooShort': 'ワークスペースのアドレスは 3 文字以上です。',
	'marketing.findWorkspace.problem.tooLong': 'ワークスペースのアドレスは 30 文字以内です。',
	'marketing.findWorkspace.problem.endHyphen': 'ワークスペースのアドレスはハイフンで終わることはできません。',
	'marketing.findWorkspace.problem.doubleHyphen': 'ワークスペースのアドレスにハイフンを 2 つ続けることはできません。',
	'marketing.findWorkspace.ownDomain': '貴社独自のアドレスのようです。{link}',
	'marketing.findWorkspace.ownDomainLink': '{host} でサインイン',
	'marketing.findWorkspace.configError':
		'サインイン情報を読み込めませんでした。接続を確認して、もう一度お試しください。',
	'marketing.findWorkspace.retry': '再試行',
	'marketing.findWorkspace.lost':
		'見つからない場合は、すでにサインインしている同僚に聞いてください。ブラウザのアドレスバーに表示されています。',
	'marketing.findWorkspace.noWorkspace': '{product} は初めてですか？ {create}',
	'marketing.findWorkspace.panelHeading': 'アドレスをお尋ねする理由',
	'marketing.findWorkspace.panelBody':
		'各社にはそれぞれ専用のワークスペースとサインインがあります。メールアドレスからワークスペースを検索することは一切ないため、このページを使って誰がどこで働いているかを調べることはできません。'
} satisfies MarketingMessages;
