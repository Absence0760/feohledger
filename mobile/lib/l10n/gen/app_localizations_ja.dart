// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for Japanese (`ja`).
class AppLocalizationsJa extends AppLocalizations {
  AppLocalizationsJa([String locale = 'ja']) : super(locale);

  @override
  String get navDashboard => 'ダッシュボード';

  @override
  String get navInvoices => '請求書';

  @override
  String get navContracts => '契約';

  @override
  String get navApprovals => '承認';

  @override
  String get navExceptions => '例外';

  @override
  String get navVendors => '取引先';

  @override
  String get navPay => '支払';

  @override
  String get navPayments => '支払い';

  @override
  String get navSettings => '設定';

  @override
  String get shellAppName => 'FeohLedger';

  @override
  String get commonSave => '保存';

  @override
  String get commonSaving => '保存中…';

  @override
  String get commonCancel => 'キャンセル';

  @override
  String get commonLoading => '読み込み中…';

  @override
  String get commonRetry => '再試行';

  @override
  String get commonAll => 'すべて';

  @override
  String get commonSearch => '検索';

  @override
  String get commonClear => 'クリア';

  @override
  String get commonApply => '適用';

  @override
  String get commonClose => '閉じる';

  @override
  String get settingsTitle => '設定';

  @override
  String get settingsTenant => 'テナント';

  @override
  String get settingsTenantNotSet => '未設定';

  @override
  String get settingsApiServer => 'APIサーバー';

  @override
  String get settingsBiometricUnlock => '生体認証ロック解除';

  @override
  String get settingsBiometricHint => '指紋または顔でロックを解除します';

  @override
  String get settingsSignOut => 'サインアウト';

  @override
  String get settingsLanguage => '言語';

  @override
  String get settingsLanguageHint => 'アプリ全体で使用する言語を選択してください。選択内容はこの端末に保存されます。';

  @override
  String get settingsLanguageSystem => 'システムの既定値';

  @override
  String get dashboardTitle => 'ダッシュボード';

  @override
  String get dashboardTotalInvoices => '請求書合計';

  @override
  String get dashboardUpcoming => '予定';

  @override
  String get dashboardForReview => '確認待ち';

  @override
  String get dashboardApproved => '承認済み';

  @override
  String get dashboardAging => '請求書の経過日数';

  @override
  String get dashboardTopVendors => '上位の取引先';

  @override
  String get dashboardAgingCurrent => '期限内';

  @override
  String get dashboardAgingDays30 => '30日';

  @override
  String get dashboardAgingDays60 => '60日';

  @override
  String get dashboardAgingDays90plus => '90日以上';

  @override
  String get dashboardCachedBanner => 'キャッシュデータを表示中 — サーバーに接続できませんでした';

  @override
  String dashboardErrorPrefix(String error) {
    return 'エラー: $error';
  }

  @override
  String dashboardInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count件の請求書',
    );
    return '$_temp0';
  }

  @override
  String get partialConversionCurrencyFallback => '報告通貨';

  @override
  String dashboardKpiUnconverted(String label, int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count 件',
    );
    return '$label：一部未換算 — $currency への為替レートがない請求書 $_temp0 を額面のまま計上しているため、合計にはその分だけ通貨が混在しています。';
  }

  @override
  String dashboardAgingUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count 件',
    );
    return '一部未換算：$currency への為替レートがない請求書 $_temp0 を額面のまま計上しているため、下の各区分はその分だけ通貨が混在しています。';
  }

  @override
  String dashboardTopVendorsUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count 件',
    );
    return '一部未換算：$currency への為替レートがない請求書 $_temp0 を額面のまま計上しているため、これらの取引先は同一通貨で並べられていません。順位を判断する前に不足しているレートを登録してください。';
  }

  @override
  String dashboardVendorFaceValueCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: 'うち $count 件は額面のまま',
    );
    return '$_temp0';
  }

  @override
  String get invoicesTitle => '請求書';

  @override
  String get invoicesSearchHint => '請求書を検索…';

  @override
  String get invoicesSearchAria => '請求書を検索';

  @override
  String get invoicesAdvancedSearch => '詳細検索';

  @override
  String get invoicesAdvancedSearchActive => '詳細検索、フィルター適用中';

  @override
  String get invoicesCaptureInvoice => '請求書を撮影';

  @override
  String get invoicesCaptureInvoiceLabel => '請求書を撮影';

  @override
  String get invoicesEmpty => '請求書が見つかりません';

  @override
  String get invoicesFilterAll => 'すべて';

  @override
  String get invoicesFilterNew => '新規';

  @override
  String get invoicesFilterPending => '保留中';

  @override
  String get invoicesFilterReview => '確認';

  @override
  String get invoicesFilterApproved => '承認済み';

  @override
  String get invoicesFilterRejected => '却下';

  @override
  String get invoicesFilterPaid => '支払済み';

  @override
  String get invoicesColInvoiceNumber => '請求書番号';

  @override
  String get invoicesColVendor => '取引先';

  @override
  String get invoicesColAmount => '金額';

  @override
  String get invoicesColDueDate => '支払期限';

  @override
  String get invoicesColStatus => 'ステータス';

  @override
  String get notificationsTitle => '通知';

  @override
  String get notificationsMarkAllRead => 'すべて既読にする';

  @override
  String get notificationsMarkAllReadLabel => 'すべての通知を既読にする';

  @override
  String get notificationsFilterUnread => '未読';

  @override
  String get notificationsAllMarkedRead => 'すべての通知を既読にしました';

  @override
  String get notificationsCouldNotMarkAll => 'すべてを既読にできませんでした';

  @override
  String get notificationsEmptyUnread => '未読の通知はありません';

  @override
  String get notificationsEmpty => '通知はありません';

  @override
  String get notificationsCaughtUp => 'すべて確認済みです';

  @override
  String get notificationsNothingYet => 'まだ何もありません';

  @override
  String get notificationsLoadError => '通知を読み込めませんでした';

  @override
  String get vendorsTitle => '取引先';

  @override
  String get vendorsSyncErp => 'ERPから同期';

  @override
  String get vendorsSyncErpLabel => 'ERPから取引先を同期';

  @override
  String get vendorsSearchHint => '取引先を検索…';

  @override
  String get vendorsFilterUnverified => '未確認';

  @override
  String get vendorsFilterActive => '有効';

  @override
  String get vendorsFilterInactive => '無効';

  @override
  String get vendorsFilterRejected => '却下';

  @override
  String get vendorsEmpty => '取引先が見つかりません';

  @override
  String get vendorsLoadError => '取引先を読み込めませんでした';

  @override
  String get vendorActionVerify => '確認';

  @override
  String get vendorActionReject => '却下';

  @override
  String get vendorUnverifiedLabel => '未確認の取引先';

  @override
  String get vendorVerifyHint => '支払い対象にする';

  @override
  String get vendorRejectHint => '無効／重複としてマーク';

  @override
  String get vendorVerified => '取引先を確認しました';

  @override
  String get vendorRejected => '取引先を却下しました';

  @override
  String get vendorActionFailed => '操作に失敗しました';

  @override
  String vendorSyncFailed(String error) {
    return 'ERP同期に失敗しました: $error';
  }

  @override
  String get exceptionsTitle => '例外';

  @override
  String get exceptionsFilterOpen => '未処理';

  @override
  String get exceptionsFilterEscalated => 'エスカレーション済み';

  @override
  String get exceptionsFilterResolved => '解決済み';

  @override
  String get exceptionsFilterDismissed => '却下済み';

  @override
  String get exceptionsEmpty => '例外はありません';

  @override
  String get exceptionsQueueClear => '例外キューは空です';

  @override
  String get exceptionActionResolve => '解決';

  @override
  String get exceptionActionEscalate => 'エスカレーション';

  @override
  String get exceptionActionDismiss => '却下';

  @override
  String get exceptionResolved => '例外を解決しました';

  @override
  String get exceptionEscalated => '例外をエスカレーションしました';

  @override
  String get exceptionDismissed => '例外を却下しました';

  @override
  String get exceptionActionFailed => '操作に失敗しました';

  @override
  String get paymentsTitle => '支払い';

  @override
  String get paymentsEmpty => '支払いはありません';

  @override
  String paymentsErrorPrefix(String error) {
    return 'エラー: $error';
  }

  @override
  String get paymentStatusPending => '保留中';

  @override
  String get paymentStatusProcessing => '処理中';

  @override
  String get paymentStatusCompleted => '完了';

  @override
  String get paymentStatusFailed => '失敗';

  @override
  String get paymentStatusCancelled => 'キャンセル済み';

  @override
  String get approvalsTitle => '承認待ち';

  @override
  String get approvalsAllCaughtUp => 'すべて完了しました！';

  @override
  String get approvalsNoneWaiting => '承認待ちの請求書はありません';

  @override
  String get approvalsLoadError => '承認待ちの請求書を読み込めませんでした';

  @override
  String approvalsPendingCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count件の請求書が承認待ち',
    );
    return '$_temp0';
  }

  @override
  String get approvalActionApprove => '承認';

  @override
  String get approvalActionReject => '却下';

  @override
  String get approvalApproved => '請求書を承認しました';

  @override
  String get captureTitle => '請求書を撮影';

  @override
  String get captureChange => '変更';

  @override
  String get captureUpload => 'アップロード';

  @override
  String get captureUploading => 'アップロード中…';

  @override
  String get captureEmptyPrompt => '写真を撮影、ギャラリーから選択、またはファイルを選択してください';

  @override
  String get captureCamera => 'カメラ';

  @override
  String get captureGallery => 'ギャラリー';

  @override
  String get captureChooseFile => 'ファイルを選択';

  @override
  String get captureSupportedFormats => 'PDF、PNG、JPG、TIFFに対応';

  @override
  String get captureUploadSuccess => '請求書を正常にアップロードしました';

  @override
  String captureUploadFailedStatus(int status, String message) {
    return 'アップロードに失敗しました（$status）: $message';
  }

  @override
  String captureUploadFailed(String error) {
    return 'アップロードに失敗しました: $error';
  }

  @override
  String captureSelectedDocument(String name) {
    return '選択した書類: $name';
  }

  @override
  String get capturePdfReady => 'PDF書類をアップロードする準備ができました';

  @override
  String get advSearchTitle => '詳細検索';

  @override
  String get advSearchClose => '詳細検索を閉じる';

  @override
  String get advSearchVendor => '取引先';

  @override
  String get advSearchPoNumber => '発注番号';

  @override
  String get advSearchMinAmount => '最小金額';

  @override
  String get advSearchMaxAmount => '最大金額';

  @override
  String get advSearchDueFrom => '期限開始';

  @override
  String get advSearchDueTo => '期限終了';

  @override
  String get advSearchAny => '指定なし';

  @override
  String get advSearchInvalidAmount => '有効な金額を入力してください（例: 1000）';

  @override
  String get advSearchMinMaxError => '最小値は最大値を超えてはいけません';

  @override
  String advSearchClearField(String label) {
    return '$labelをクリア';
  }

  @override
  String advSearchDateFieldHint(String label, String value) {
    return '$label、現在 $value。ダブルタップで変更します。';
  }

  @override
  String get invoiceDetailTitle => '請求書の詳細';

  @override
  String get invoiceDetailEdit => '編集';

  @override
  String get invoiceDetailEditLabel => '請求書を編集';

  @override
  String get invoiceDetailRetry => '再試行';

  @override
  String invoiceDetailErrorPrefix(String error) {
    return 'エラー: $error';
  }

  @override
  String get invoiceDetailNoChanges => '保存する変更はありません';

  @override
  String get invoiceDetailUpdated => '請求書を更新しました';

  @override
  String invoiceDetailUpdateFailed(String error) {
    return '変更を保存できませんでした: $error';
  }

  @override
  String get invoiceDetailApproved => '請求書を承認しました';

  @override
  String get invoiceDetailApproveFailed => '請求書を承認できませんでした。もう一度お試しください';

  @override
  String get invoiceDetailRejected => '請求書を却下しました';

  @override
  String get invoiceDetailRejectFailed => '請求書を却下できませんでした。もう一度お試しください';

  @override
  String get invoiceDetailRejectTitle => '請求書を却下';

  @override
  String get invoiceDetailRejectReason => '理由';

  @override
  String get invoiceDetailReject => '却下';

  @override
  String get invoiceDetailApprove => '承認';

  @override
  String get invoiceDetailUnknownVendor => '不明な取引先';

  @override
  String get invoiceDetailFieldInvoiceNumber => '請求書番号';

  @override
  String get invoiceDetailFieldPoNumber => '発注番号';

  @override
  String get invoiceDetailFieldCurrency => '通貨';

  @override
  String get invoiceDetailFieldInvoiceDate => '請求日';

  @override
  String get invoiceDetailFieldDueDate => '支払期日';

  @override
  String get invoiceDetailFieldDescription => '説明';

  @override
  String get invoiceDetailFieldGlAccount => '勘定科目';

  @override
  String get invoiceDetailFieldCreated => '作成日';

  @override
  String get invoiceDetailActivity => 'アクティビティ';

  @override
  String get invoiceDetailActivityError => 'アクティビティを読み込めませんでした';

  @override
  String get invoiceDetailFilePdfLabel => '請求書PDF。ダブルタップで全画面表示します。';

  @override
  String get invoiceDetailFileLabel => '請求書ファイル。ダブルタップで全画面表示します。';

  @override
  String get invoiceDetailTapToViewPdf => 'タップしてPDFを表示';

  @override
  String get invoiceDetailTapToViewFile => 'タップしてファイルを表示';

  @override
  String get invoiceEditTitle => '請求書を編集';

  @override
  String get invoiceEditClose => '編集フォームを閉じる';

  @override
  String get invoiceEditVendor => '取引先';

  @override
  String get invoiceEditInvoiceNumber => '請求書番号';

  @override
  String get invoiceEditAmount => '金額';

  @override
  String get invoiceEditPoNumber => '発注番号';

  @override
  String get invoiceEditGlAccount => '勘定科目';

  @override
  String get invoiceEditDescription => '説明';

  @override
  String get invoiceEditDueDate => '支払期日';

  @override
  String get invoiceEditNotSet => '未設定';

  @override
  String get invoiceEditInvalidAmount => '有効な金額を入力してください（例: 1234.56）';

  @override
  String get invoiceEditClearDueDate => '支払期日をクリア';

  @override
  String get invoiceEditLockedNotice =>
      '承認済み — 支払先と金額は固定されています。変更するには請求書を却下し、修正してから再承認してください。';

  @override
  String get invoiceEditLockedHelper => '承認後は変更不可';

  @override
  String invoiceEditDueDateHint(String value) {
    return '支払期日、現在 $value。ダブルタップで変更します。';
  }

  @override
  String get warningsSectionTitle => '警告と不正フラグ';

  @override
  String get warningsPoMatchTitle => '発注照合';

  @override
  String get warningsSeverityError => 'エラー';

  @override
  String get warningsSeverityWarning => '警告';

  @override
  String get warningsSeverityInfo => '情報';

  @override
  String get warningsPoLabel => '発注';

  @override
  String warningsMatchLabel(String type) {
    return '$type照合';
  }

  @override
  String warningsVarianceLabel(String value) {
    return '$value% の差異';
  }

  @override
  String get erpStatusTitle => 'ERPステータス';

  @override
  String get erpStatusReference => 'ERP参照番号';

  @override
  String get erpStatusDocumentId => 'ドキュメントID';

  @override
  String get erpStatusError => 'エラー';

  @override
  String get erpStatusLastUpdate => '最終更新';

  @override
  String get erpStatusStatus => 'ステータス';

  @override
  String get fileViewerPdfTitle => '請求書PDF';

  @override
  String get fileViewerImageTitle => '請求書画像';

  @override
  String get fileViewerPdfError => 'PDFを読み込めませんでした';

  @override
  String get fileViewerImageError => '画像を読み込めませんでした';

  @override
  String get fileViewerRetry => '再試行';

  @override
  String get timelineNoActivity => 'まだアクティビティはありません';

  @override
  String get payTitle => '支払';

  @override
  String get payTabQueue => 'キュー';

  @override
  String get payTabRuns => '実行';

  @override
  String get paySummaryTotalPaid => '支払合計';

  @override
  String get paySummaryPending => '保留中';

  @override
  String get paySummaryInQueue => 'キュー内';

  @override
  String get paySummaryCardRebates => 'カードリベート';

  @override
  String paySummaryPaymentsSubtitle(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count件の支払',
    );
    return '$_temp0';
  }

  @override
  String get payQueueEmpty => '支払待ちの請求書はありません';

  @override
  String get payQueueError => '支払キューを読み込めませんでした';

  @override
  String get payQueueRetry => '再試行';

  @override
  String payQueueDue(String date) {
    return '期日 $date';
  }

  @override
  String get payQueueNoDueDate => '支払期日なし';

  @override
  String payQueueDiscount(String amount) {
    return '割引 $amount';
  }

  @override
  String get payQueueOverdue => '期限超過';

  @override
  String get payQueueSelected => '選択済み';

  @override
  String get payQueueBlockedDuplicate => '重複の可能性 — 未解決';

  @override
  String get payQueueBlockedFraudFlag => '不正フラグ — 未解決';

  @override
  String get payQueueBlockedLineTotalMismatch => '明細合計が一致しません — 未解決';

  @override
  String get payQueueBlockedPaymentReconciliation =>
      '過去の支払いが未照合 — 処理中の可能性があります';

  @override
  String get payQueueBlockedFullyCredited => 'クレジットメモで全額相殺済み — 支払額はありません';

  @override
  String get payQueueBlockedLiveVirtualCard =>
      '有効なバーチャルカードがこの請求書を押さえています — カードで支払ってください';

  @override
  String get payQueueBlockedGeneric => '未解決の例外が支払いをブロックしています';

  @override
  String payQueueBlockedAnnounce(String reason) {
    return '支払えません: $reason';
  }

  @override
  String payQueuePinnedMethod(String method) {
    return '$method で支払う';
  }

  @override
  String payMethodLabel(String invoiceNumber) {
    return '$invoiceNumber の支払方法';
  }

  @override
  String get payMethodAch => 'ACH';

  @override
  String get payMethodWire => '電信送金';

  @override
  String get payMethodCheck => '小切手';

  @override
  String get payMethodVirtualCard => 'バーチャルカード';

  @override
  String paySelectedCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count件の請求書を選択中',
    );
    return '$_temp0';
  }

  @override
  String get payClear => 'クリア';

  @override
  String get payCreateRun => '実行を作成';

  @override
  String payCreateRunFailed(String error) {
    return '実行の作成に失敗しました: $error';
  }

  @override
  String get payRunsEmpty => '支払実行はありません';

  @override
  String payRunSubtitle(int count, String date) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count件の支払',
    );
    return '$_temp0 • $date';
  }

  @override
  String get payRunCfoRequiredSuffix => ' • CFOの承認が必要';

  @override
  String payRunAnnounce(String amount, String status, String subtitle) {
    return '実行 $amount、$status、$subtitle';
  }

  @override
  String get payRunActions => '実行アクション';

  @override
  String get payRunActionExecute => '実行';

  @override
  String get payRunActionCancel => 'キャンセル';

  @override
  String get payRunActionApprove => 'CFOとして承認';

  @override
  String get payRunApproveTitle => '支払実行を承認しますか？';

  @override
  String payRunApproveBody(String date, int count, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count件の支払',
    );
    return '$date に作成された実行の承認 — $_temp0、合計 $amount。これは実行を承認するものであり、送金は行いません。';
  }

  @override
  String get payRunApproveConfirm => '承認';

  @override
  String payRunApproveFailed(String error) {
    return '承認に失敗しました: $error';
  }

  @override
  String get payRunCfoBlocked => 'この実行を行うにはCFOの承認が必要です。';

  @override
  String get payRunExecuteTitle => '支払実行を行いますか？';

  @override
  String payRunExecuteBody(String amount) {
    return '設定された決済プロセッサーを通じて $amount を送金します。';
  }

  @override
  String payRunExecuteFailed(String error) {
    return '実行に失敗しました: $error';
  }

  @override
  String payRunCancelFailed(String error) {
    return 'キャンセルに失敗しました: $error';
  }

  @override
  String get payRunStatusDraft => '下書き';

  @override
  String get payRunStatusCompleted => '完了';

  @override
  String get payRunStatusSubmitted => '送信済み';

  @override
  String get payRunStatusPartial => '一部';

  @override
  String get payRunStatusFailed => '失敗';

  @override
  String get payRunStatusCancelled => 'キャンセル済み';

  @override
  String get payConfirmCancel => 'キャンセル';

  @override
  String get payConfirmExecute => '実行';

  @override
  String get loginAppName => 'FeohLedger';

  @override
  String get loginTagline => '買掛金管理をシンプルに';

  @override
  String get loginTenant => 'テナント';

  @override
  String get loginEmail => 'メールアドレス';

  @override
  String get loginPassword => 'パスワード';

  @override
  String get loginShowPassword => 'パスワードを表示';

  @override
  String get loginHidePassword => 'パスワードを非表示';

  @override
  String get loginRequired => '必須';

  @override
  String get loginSignIn => 'サインイン';

  @override
  String get mfaTitle => '二要素認証';

  @override
  String get mfaHeading => '本人確認';

  @override
  String get mfaPromptEmail => 'メールで送信した6桁のコードを入力してください。';

  @override
  String get mfaPromptTotp => '認証アプリの6桁のコードを入力してください。';

  @override
  String get mfaEnforcedNotice =>
      '組織で二要素認証が必須となっています。今はメールコードで認証し、後でWebアプリで認証アプリの設定を完了してください。';

  @override
  String get mfaCode => 'コード';

  @override
  String get mfaCodeRequired => '必須';

  @override
  String get mfaCodeTooShort => '6桁以上入力してください';

  @override
  String get mfaVerify => '確認';

  @override
  String get mfaSending => '送信中…';

  @override
  String get mfaResendEmailCode => 'メールコードを再送信';

  @override
  String get mfaSendEmailCode => 'メールコードを送信';

  @override
  String get mfaUseEmailInstead => '代わりにメールコードを使用';

  @override
  String get mfaUseAuthenticatorInstead => '代わりに認証アプリを使用';

  @override
  String get mfaEmailedAnnounce => 'サインインコードをメールで送信しました。';

  @override
  String get adminUsersTitle => 'ユーザー管理';

  @override
  String get adminUsersSearchHint => '名前またはメールで検索';

  @override
  String get adminUsersEmpty => 'ユーザーが見つかりません';

  @override
  String get adminUsersLoadError => 'ユーザーを読み込めませんでした';

  @override
  String get adminUsersEditRoles => 'ロールを編集';

  @override
  String get adminUsersNoRoles => 'ロールなし';

  @override
  String get adminUsersDeactivate => 'ユーザーを無効化';

  @override
  String get adminUsersActivate => 'ユーザーを有効化';

  @override
  String get adminUsersCannotDeactivateSelf => '自分のアカウントは無効化できません';

  @override
  String get adminUsersDeactivateHint => 'サインアウトさせ、サインインをブロックします';

  @override
  String get adminUsersActivateHint => 'サインインアクセスを復元します';

  @override
  String get adminUsersRoleActive => '有効';

  @override
  String get adminUsersRoleInactive => '無効';

  @override
  String get adminUsersInactiveBadge => '無効';

  @override
  String adminUsersRolesUpdated(String name) {
    return '$name のロールを更新しました';
  }

  @override
  String adminUsersRolesUpdateFailed(String error) {
    return 'ロールの更新に失敗しました: $error';
  }

  @override
  String adminUsersActivated(String name) {
    return '$name を有効化しました';
  }

  @override
  String adminUsersDeactivated(String name) {
    return '$name を無効化しました';
  }

  @override
  String adminUsersUpdateFailed(String name, String error) {
    return '$name の更新に失敗しました: $error';
  }

  @override
  String get adminUsersCreateUser => 'ユーザーを作成';

  @override
  String get adminUsersCreateTitle => '新規ユーザー';

  @override
  String get adminUsersFieldFullName => '氏名';

  @override
  String get adminUsersFieldEmail => 'メールアドレス';

  @override
  String get adminUsersFieldRoles => 'ロール';

  @override
  String get adminUsersValidationNameRequired => '氏名は必須です';

  @override
  String get adminUsersValidationEmailRequired => 'メールアドレスは必須です';

  @override
  String get adminUsersValidationEmailInvalid => '有効なメールアドレスを入力してください';

  @override
  String get adminUsersCreateSubmit => '作成';

  @override
  String get adminUsersCreating => '作成中…';

  @override
  String adminUsersCreated(String name) {
    return '$name を作成しました';
  }

  @override
  String adminUsersCreateFailed(String error) {
    return 'ユーザーの作成に失敗しました: $error';
  }

  @override
  String get adminUsersTempPasswordTitle => 'ユーザーを作成しました';

  @override
  String adminUsersTempPasswordBody(String name) {
    return 'このワンタイムパスワードを $name に共有してください。初回サインイン時に変更を求められます。再表示されません。';
  }

  @override
  String get adminUsersDelete => 'ユーザーを削除';

  @override
  String get adminUsersDeleteHint => 'このアカウントを完全に削除します';

  @override
  String get adminUsersCannotDeleteSelf => '自分のアカウントは削除できません';

  @override
  String adminUsersDeleteConfirmTitle(String name) {
    return '$name を削除しますか？';
  }

  @override
  String adminUsersDeleteConfirmBody(String name, String email) {
    return '$name（$email）を完全に削除します。元に戻せません。';
  }

  @override
  String adminUsersDeleted(String name) {
    return '$name を削除しました';
  }

  @override
  String adminUsersDeleteFailed(String name, String error) {
    return '$name の削除に失敗しました: $error';
  }

  @override
  String get orgSettingsTitle => '組織設定';

  @override
  String get orgSettingsNoSettings => '設定がありません';

  @override
  String get orgSettingsLoadError => '設定を読み込めませんでした';

  @override
  String get orgSettingsSectionCompany => '会社';

  @override
  String get orgSettingsSectionInvoiceDefaults => '請求書のデフォルト';

  @override
  String get orgSettingsName => '組織名';

  @override
  String get orgSettingsAddress => '住所';

  @override
  String get orgSettingsPhone => '電話番号';

  @override
  String get orgSettingsWebsite => 'ウェブサイト';

  @override
  String get orgSettingsTaxId => '税務ID';

  @override
  String get orgSettingsCurrency => 'デフォルト通貨';

  @override
  String get orgSettingsPaymentTerms => '支払条件';

  @override
  String get orgSettingsNumberPrefix => '請求書番号のプレフィックス';

  @override
  String get orgSettingsGlAccount => 'デフォルトの総勘定元帳アカウント';

  @override
  String get orgSettingsCostCenter => 'デフォルトのコストセンター';

  @override
  String get orgSettingsSave => '変更を保存';

  @override
  String get orgSettingsSaving => '保存中…';

  @override
  String orgSettingsFieldRequired(String label) {
    return '$label は必須です';
  }

  @override
  String get orgSettingsSaved => '組織設定を保存しました';

  @override
  String orgSettingsSaveFailed(String error) {
    return '保存に失敗しました: $error';
  }

  @override
  String get workflowsTitle => 'ワークフロー';

  @override
  String get workflowsEmpty => 'ワークフローが見つかりません';

  @override
  String get workflowsLoadError => 'ワークフローを読み込めませんでした';

  @override
  String get workflowsStatusActive => '有効';

  @override
  String get workflowsStatusInactive => '無効';

  @override
  String get workflowsDefault => 'デフォルト';

  @override
  String workflowsStepCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$countステップ',
    );
    return '$_temp0';
  }

  @override
  String get workflowDetailFallbackTitle => 'ワークフロー';

  @override
  String get workflowDetailLoadError => 'ワークフローを読み込めませんでした';

  @override
  String get workflowDetailNoSteps => 'このワークフローにはステップがありません。';

  @override
  String get workflowDetailDefaultWorkflow => 'デフォルトのワークフロー';

  @override
  String workflowDetailStepNumber(int number) {
    return 'ステップ $number';
  }

  @override
  String get workflowDetailStepEnabled => '有効';

  @override
  String get workflowDetailStepDisabled => '無効';

  @override
  String workflowDetailApproverCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '承認者$count人',
    );
    return '$_temp0';
  }

  @override
  String workflowDetailDelaySummary(String hours) {
    return '遅延 $hours 時間';
  }

  @override
  String workflowDetailConditionSummary(String field) {
    return '$field で';
  }

  @override
  String get cashFlowTitle => 'キャッシュフロー予測';

  @override
  String cashFlowErrorPrefix(String error) {
    return 'エラー: $error';
  }

  @override
  String cashFlowHorizonDays(int days) {
    return '$days日間';
  }

  @override
  String get cashFlowLowBalanceAlert => '残高不足の警告';

  @override
  String cashFlowBreachSingle(
    String threshold,
    String period,
    String shortfall,
  ) {
    return '$periodに残高が$thresholdを下回る見込みです（不足額 $shortfall）。';
  }

  @override
  String cashFlowBreachMultiple(int count, String period, String shortfall) {
    return '$count期間で最低残高を下回る見込みです。最悪は$period、不足額 $shortfall。';
  }

  @override
  String get cashFlowMinimum => '最低';

  @override
  String get cashFlowOpeningBalance => '期首残高';

  @override
  String get cashFlowProjectedEnd => '予測期末残高';

  @override
  String cashFlowProjectedEndSubtitle(int days) {
    return '$days日後';
  }

  @override
  String get cashFlowCommittedOut => '確定支出';

  @override
  String get cashFlowCommittedSubtitle => '確定済みの支払い';

  @override
  String get cashFlowPendingOut => '保留中支出';

  @override
  String get cashFlowPendingSubtitle => '処理中のパイプライン';

  @override
  String get cashFlowOpeningSourceProvider => '銀行から同期';

  @override
  String get cashFlowOpeningSourceSettings => '保存済み残高';

  @override
  String get cashFlowOpeningSourceQuery => '手動';

  @override
  String get cashFlowOpeningSourceUnset => '残高を設定';

  @override
  String get cashFlowProjectedOutflows => '予測支出';

  @override
  String get cashFlowNoOutflows => 'この期間に予測される支出はありません。';

  @override
  String cashFlowInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '請求書$count件',
    );
    return '$_temp0';
  }

  @override
  String cashFlowCommittedAmount(String amount) {
    return '確定 $amount';
  }

  @override
  String cashFlowPendingAmount(String amount) {
    return '保留中 $amount';
  }

  @override
  String get cashFlowPosition => '資金ポジション';

  @override
  String get cashFlowNoPosition => 'この期間の資金ポジション予測はありません。';

  @override
  String cashFlowOutAmount(String amount) {
    return '支出 $amount';
  }

  @override
  String cashFlowForecastRowLabel(
    String period,
    String scheduled,
    String committed,
    String pending,
    int count,
  ) {
    return '$period：予定 $scheduled、確定 $committed、保留中 $pending、請求書$count件';
  }

  @override
  String cashFlowPositionRowLabel(
    String period,
    String opening,
    String outflow,
    String closing,
  ) {
    return '$period：期首 $opening、支出 $outflow、期末 $closing';
  }

  @override
  String get cashFlowBelowThresholdSuffix => '、しきい値未満';

  @override
  String cashFlowLowBalanceAlertLabel(String message) {
    return '残高不足の警告。$message';
  }

  @override
  String cashFlowForecastUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count 件',
    );
    return '一部未換算：$currency への為替レートがない請求書 $_temp0 を額面のまま計上しているため、これらの支出と上の確定・保留中の合計には、その分だけ通貨が混在しています。';
  }

  @override
  String cashFlowPositionUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count 件',
    );
    return '未換算：$currency への為替レートがない請求書 $_temp0 を額面のまま集計しているため、下の残高推移と上の予測期末残高には複数通貨が混在しています。これらの数値をもとに判断する前に、不足しているレートを登録してください。';
  }

  @override
  String get contractsTitle => '契約';

  @override
  String get contractsSearchHint => '契約を検索...';

  @override
  String get contractsEmpty => '契約が見つかりません';

  @override
  String get contractsFilterDraft => '下書き';

  @override
  String get contractsFilterActive => '有効';

  @override
  String get contractsFilterExpired => '期限切れ';

  @override
  String get contractsFilterTerminated => '解約済み';

  @override
  String get contractsFilterCancelled => 'キャンセル済み';

  @override
  String get contractDetailTitle => '契約の詳細';

  @override
  String contractDetailErrorPrefix(String error) {
    return 'エラー: $error';
  }

  @override
  String get contractDetailUntitled => '無題の契約';

  @override
  String get contractDetailFieldContractNumber => '契約番号';

  @override
  String get contractDetailFieldVendor => 'ベンダー';

  @override
  String get contractDetailFieldType => '種類';

  @override
  String get contractDetailFieldCurrency => '通貨';

  @override
  String get contractDetailFieldSpendLimit => '支出上限';

  @override
  String get contractDetailNotToExceed => '（上限額）';

  @override
  String get contractDetailFieldStartDate => '開始日';

  @override
  String get contractDetailFieldEndDate => '終了日';

  @override
  String get contractDetailFieldSigned => '署名日';

  @override
  String get contractDetailFieldAutoRenew => '自動更新';

  @override
  String get contractDetailYes => 'はい';

  @override
  String get contractDetailNo => 'いいえ';

  @override
  String get contractDetailFieldRenewalTerm => '更新期間';

  @override
  String contractDetailRenewalTermMonths(int months) {
    return '$monthsか月';
  }

  @override
  String get contractDetailFieldRenewalNotice => '更新通知';

  @override
  String contractDetailRenewalNoticeDays(int days) {
    return '$days日';
  }

  @override
  String get contractDetailFieldPaymentTerms => '支払条件';

  @override
  String get contractDetailFieldDescription => '説明';

  @override
  String get contractDetailFieldCreated => '作成日';

  @override
  String get contractDetailSectionSpend => '支出';

  @override
  String get contractDetailSectionLineItems => '明細';

  @override
  String get contractDetailSpendInvoiced => '請求済み';

  @override
  String contractDetailSpendInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '請求書$count件',
    );
    return '$_temp0';
  }

  @override
  String get contractDetailSpendOverLimit => '上限超過';

  @override
  String get contractDetailSpendRemaining => '残り';

  @override
  String contractDetailSpendOfLimit(String limit) {
    return '上限 $limit のうち';
  }

  @override
  String get contractDetailSpendNoLimit => '上限なし';

  @override
  String get contractDetailLineItemFallback => '明細項目';

  @override
  String contractDetailLineQty(String quantity) {
    return '数量 $quantity';
  }

  @override
  String contractDetailLineUnitPrice(String price) {
    return '@ $price';
  }

  @override
  String contractDetailLineGl(String account) {
    return '勘定科目 $account';
  }

  @override
  String get contractActivate => '有効化';

  @override
  String get contractActivated => '契約を有効化しました';

  @override
  String get contractActivateFailed => '契約を有効化できませんでした。もう一度お試しください';

  @override
  String get contractTerminate => '解約';

  @override
  String get contractTerminateTitle => '契約の解約';

  @override
  String get contractTerminateBody => '契約を早期に終了します。この操作は取り消せません。続行しますか？';

  @override
  String get contractTerminated => '契約を解約しました';

  @override
  String get contractTerminateFailed => '契約を解約できませんでした。もう一度お試しください';

  @override
  String get exceptionDetailTitle => '例外';

  @override
  String get exceptionDetailNotFound => '例外が見つかりません';

  @override
  String get exceptionDetailOverdue => '期限超過';

  @override
  String get exceptionDetailSectionDescription => '説明';

  @override
  String get exceptionDetailSectionInvoice => '請求書';

  @override
  String get exceptionDetailNoLinkedInvoice => '関連する請求書はありません';

  @override
  String get exceptionDetailFieldNumber => '番号';

  @override
  String get exceptionDetailFieldVendor => 'ベンダー';

  @override
  String get exceptionDetailFieldAmount => '金額';

  @override
  String get exceptionDetailFieldSeverity => '深刻度';

  @override
  String get exceptionDetailSectionSla => 'SLA';

  @override
  String get exceptionDetailFieldCreated => '作成日';

  @override
  String get exceptionDetailFieldDue => '期限';

  @override
  String get exceptionDetailNoSla => 'SLAが設定されていません';

  @override
  String get exceptionDetailFieldStatus => 'ステータス';

  @override
  String get exceptionDetailOnTrack => '順調';

  @override
  String get exceptionDetailResolvedIn => '解決までの時間';

  @override
  String exceptionDetailResolvedInHours(String hours) {
    return '$hours 時間';
  }

  @override
  String get exceptionDetailSectionAssignee => '担当者';

  @override
  String get exceptionDetailUnassigned => '未割り当て';

  @override
  String get exceptionDetailAssign => '割り当て';

  @override
  String get exceptionDetailReassign => '再割り当て';

  @override
  String get exceptionDetailSectionResolution => '解決';

  @override
  String get exceptionDetailResolutionNote => 'メモ';

  @override
  String get exceptionDetailResolutionBy => '対応者';

  @override
  String get exceptionDetailResolutionAt => '対応日時';

  @override
  String get exceptionDetailActionResolved => '例外を解決しました';

  @override
  String get exceptionDetailActionEscalated => '例外をエスカレーションしました';

  @override
  String get exceptionDetailActionDismissed => '例外を却下しました';

  @override
  String get exceptionDetailActionResolveFailed => '例外を解決できませんでした';

  @override
  String get exceptionDetailActionEscalateFailed => '例外をエスカレーションできませんでした';

  @override
  String get exceptionDetailActionDismissFailed => '例外を却下できませんでした';

  @override
  String get exceptionDetailAssignTo => '割り当て先';

  @override
  String get exceptionDetailUnassign => '割り当て解除';

  @override
  String exceptionDetailLoadUsersFailed(String error) {
    return 'ユーザーを読み込めませんでした: $error';
  }

  @override
  String get exceptionDetailAssigneeUpdateFailed => '担当者を更新できませんでした';

  @override
  String get exceptionDetailUnassigned2 => '例外の割り当てを解除しました';

  @override
  String exceptionDetailAssignedTo(String name) {
    return '$nameに割り当てました';
  }

  @override
  String get settingsProcurement => '調達';

  @override
  String get settingsInspections => '品質検査';

  @override
  String get settingsInspectionsHint => '4 ウェイ照合の検査を登録・確認';

  @override
  String get settingsAdministration => '管理';

  @override
  String get settingsAdminUsers => 'ユーザー管理';

  @override
  String get settingsAdminUsersHint => 'ロール、ユーザーの有効化 / 無効化';

  @override
  String get settingsAdminOrg => '組織設定';

  @override
  String get settingsAdminOrgHint => '会社プロフィール、請求書の既定値';

  @override
  String get settingsAdminWorkflows => 'ワークフロー';

  @override
  String get settingsAdminWorkflowsHint => 'ワークフロー定義とステップを表示';

  @override
  String get settingsAdaptive => '適応型ワークフロー';

  @override
  String get settingsAdaptiveHint => '承認パターン、異常、推奨';

  @override
  String get inspectionsTitle => '品質検査';

  @override
  String get inspectionsRecord => '検査を登録';

  @override
  String get inspectionsEmpty => '品質検査は登録されていません。';

  @override
  String get inspectionsEmptyFiltered => 'この結果の検査はありません。';

  @override
  String get inspectionsLoadError => '検査の読み込みに失敗しました';

  @override
  String get inspectionsResultPass => '合格';

  @override
  String get inspectionsResultFail => '不合格';

  @override
  String get inspectionsResultPartial => '一部受入';

  @override
  String get inspectionsResultUnknown => '不明な結果';

  @override
  String inspectionsResultAnnounce(String result) {
    return '結果: $result';
  }

  @override
  String get inspectionsNotLinked => '未リンク';

  @override
  String get inspectionsNotLinkedHint =>
      'この検査は入荷にも発注にも紐づいていないため、発注照合が参照することはありません。';

  @override
  String get inspectionsReceiptUnnamed => '入荷番号なし';

  @override
  String get inspectionsHintPass => '受入 — 照合結果は変わりません。';

  @override
  String get inspectionsHintFail => '不受入 — 請求書は不一致となり、品質保留が支払をブロックします。';

  @override
  String get inspectionsHintPartial => '一部受入 — 照合結果は「一部」となり、受入数量が記録されます。';

  @override
  String get inspectionsHintUnknown =>
      'この結果は合格 / 不合格 / 一部受入のいずれにも該当しないため、照合への影響は示せません。';

  @override
  String inspectionsRecorded(String number) {
    return '検査 $number を登録しました';
  }

  @override
  String inspectionsRecordFailed(String error) {
    return '検査を登録できませんでした: $error';
  }

  @override
  String inspectionsReceiptsLoadFailed(String error) {
    return '入荷を読み込めませんでした: $error';
  }

  @override
  String get inspectionRecordTitle => '品質検査の登録';

  @override
  String get inspectionRecordClose => '検査フォームを閉じる';

  @override
  String get inspectionRecordReceipt => '入荷';

  @override
  String get inspectionRecordReceiptHint =>
      '発注照合は入荷を通じてのみ検査を参照するため、検査には対象の入荷を指定する必要があります。';

  @override
  String inspectionRecordReceiptsBounded(int shown, int total) {
    return '$total 件のうち最新 $shown 件の入荷を表示しています。対象の入荷が見つからない場合は Web アプリから登録してください。';
  }

  @override
  String get inspectionRecordNoReceipts =>
      '入荷がまだありません。検査は入荷に対して行うため、登録する対象がありません。';

  @override
  String get inspectionRecordNumber => '検査番号';

  @override
  String get inspectionRecordNumberRequired => '検査番号を入力してください';

  @override
  String get inspectionRecordResult => '結果';

  @override
  String get inspectionRecordAcceptedQuantity => '受入数量';

  @override
  String get inspectionRecordRejectedQuantity => '不合格数量';

  @override
  String get inspectionRecordAcceptedRequired => '一部受入の場合は必須';

  @override
  String get inspectionRecordInvalidQuantity => '整数 8 桁・小数 4 桁までで入力してください';

  @override
  String get inspectionRecordInspectedDate => '検査日';

  @override
  String get inspectionRecordDateNotSet => '未設定';

  @override
  String get inspectionRecordClearDate => '検査日をクリア';

  @override
  String get inspectionRecordInspector => '検査者';

  @override
  String get inspectionRecordNotes => '逸脱メモ';

  @override
  String get inspectionRecordNotesHint =>
      '不合格の場合、請求書の照合メッセージにそのまま引用され、品質保留を処理する担当者が読みます。';

  @override
  String get inspectionRecordSubmit => '検査を登録';

  @override
  String get inspectionDetailTitle => '検査';

  @override
  String get inspectionDetailNotFound => '検査が見つかりません';

  @override
  String inspectionDetailErrorPrefix(String error) {
    return '検査を読み込めませんでした: $error';
  }

  @override
  String get inspectionDetailFieldReceipt => '入荷';

  @override
  String get inspectionDetailFieldInspectedDate => '検査日';

  @override
  String get inspectionDetailFieldInspector => '検査者';

  @override
  String get inspectionDetailFieldAccepted => '受入数量';

  @override
  String get inspectionDetailFieldRejected => '不合格数量';

  @override
  String get inspectionDetailFieldStatus => 'ステータス';

  @override
  String get inspectionDetailFieldCreated => '作成日時';

  @override
  String get inspectionDetailSectionNotes => '逸脱メモ';

  @override
  String get adaptiveTitle => '適応型ワークフロー';

  @override
  String get adaptiveTabSuggestions => '推奨';

  @override
  String get adaptiveTabPatterns => '承認パターン';

  @override
  String get adaptiveTabAnomalies => '異常検知';

  @override
  String get adaptiveAdvisoryNote =>
      'ここに表示される内容はすべて助言です。この画面でワークフローが変更されたことはありません。推奨は、適切なロールを持つ担当者が Web アプリで適用したときに初めて有効になり、その適用は手動編集と同じ監査対象の経路を通ります。';

  @override
  String get adaptiveSuggestionsEmpty => '推奨はありません。一貫した承認履歴がまだ十分ではありません。';

  @override
  String get adaptiveSuggestionsError => '推奨を読み込めませんでした。';

  @override
  String get adaptiveSuggestionsShowOpen => '未処理';

  @override
  String get adaptiveSuggestionsShowAll => 'すべて';

  @override
  String adaptiveSuggestionsConfidence(String pct) {
    return '信頼度 $pct%';
  }

  @override
  String get adaptiveSuggestionsDismiss => '却下';

  @override
  String get adaptiveSuggestionsDismissTitle => 'この推奨を却下しますか？';

  @override
  String get adaptiveSuggestionsDismissBody =>
      '再計算後も却下状態が維持されます。いずれの場合もワークフローは変更されません。';

  @override
  String get adaptiveSuggestionsDismissed => '推奨を却下しました。';

  @override
  String adaptiveSuggestionsDismissFailed(String error) {
    return 'その推奨を却下できませんでした: $error';
  }

  @override
  String get adaptiveSuggestionStatusOpen => '未処理';

  @override
  String get adaptiveSuggestionStatusDismissed => '却下済み';

  @override
  String get adaptiveSuggestionStatusApplied => '適用済み';

  @override
  String get adaptiveSuggestionStatusStale => '陳腐化';

  @override
  String get adaptiveSuggestionStatusUnknown => '不明なステータス';

  @override
  String adaptiveSuggestionStatusAnnounce(String status) {
    return 'ステータス: $status';
  }

  @override
  String get adaptivePatternsError => '承認パターンを読み込めませんでした。';

  @override
  String get adaptivePatternsEmptyApprovers => 'この期間の承認判断はまだありません。';

  @override
  String get adaptivePatternsEmptyVendors => 'この期間の取引先の承認履歴はまだありません。';

  @override
  String get adaptivePatternsSectionApprovers => '承認者別';

  @override
  String get adaptivePatternsSectionVendors => '取引先別';

  @override
  String adaptivePatternsLookback(int days) {
    return 'このテナント自身の直近 $days 日間の承認履歴に基づく決定論的な統計です。モデルは使わず、表示ごとに再計算されます。';
  }

  @override
  String get adaptivePatternsCurrencyNote => '金額は組織の報告通貨で表示されます。';

  @override
  String get adaptivePatternsUnknownApprover => '不明な承認者';

  @override
  String adaptivePatternsApproverSummary(int approved, int rejected) {
    return '承認 $approved 件 · 却下 $rejected 件';
  }

  @override
  String adaptivePatternsApproverTiming(String rate, String days) {
    return '承認率 $rate% · 中央値 $days 日';
  }

  @override
  String adaptivePatternsVendorConsistency(String pct) {
    return '無修正での承認 $pct%';
  }

  @override
  String adaptivePatternsVendorMoney(String median, String avg) {
    return '中央値 $median · 平均 $avg';
  }

  @override
  String adaptivePatternsUnconverted(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count 件の承認を報告通貨で表現できなかったため、上の金額からは除外されています（サンプル件数には含まれます）',
    );
    return '$_temp0';
  }

  @override
  String get adaptiveAnomaliesIntro =>
      'レビュー中の請求書のうち、金額・承認者・滞留時間の面で取引先の確立したパターンから外れているものです。読み取り専用で、ここでの表示が何かを発生させたり止めたりすることはありません。';

  @override
  String get adaptiveAnomaliesEmpty => 'レビュー中で取引先の通常パターンから外れているものはありません。';

  @override
  String get adaptiveAnomaliesError => '異常スキャンを読み込めませんでした。';

  @override
  String adaptiveAnomaliesScanned(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: 'レビュー中の請求書 $count 件をスキャンしました。',
    );
    return '$_temp0';
  }

  @override
  String adaptiveAnomaliesAmount(String amount, String currency) {
    return '$amount $currency';
  }

  @override
  String get adaptiveAnomaliesInsufficient => 'この取引先の履歴はまだ十分ではありません。';

  @override
  String get invoiceWarningMissingVendorName => '仕入先名がありません';

  @override
  String get invoiceWarningMissingInvoiceNumber => '請求書番号がありません';

  @override
  String get invoiceWarningMissingAmount => '金額が未入力またはゼロです';

  @override
  String get invoiceWarningDuplicateInvoiceNumber => 'この仕入先で請求書番号が重複しています';

  @override
  String invoiceWarningDuplicateSimilar(
    String similarity,
    String invoiceNumber,
    String vendorName,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other: '（別エンティティにほぼ同一の請求書が$crossEntityCount件あります）',
      zero: '',
    );
    return '重複の可能性: $invoiceNumber（$vendorName）と$similarity一致$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarUnnamedVendor(
    String similarity,
    String invoiceNumber,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other: '（別エンティティにほぼ同一の請求書が$crossEntityCount件あります）',
      zero: '',
    );
    return '重複の可能性: $invoiceNumberと$similarity一致$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarUnnumbered(
    String similarity,
    String vendorName,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other: '（別エンティティにほぼ同一の請求書が$crossEntityCount件あります）',
      zero: '',
    );
    return '重複の可能性: $vendorNameの別の請求書と$similarity一致$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarUnnumberedUnnamedVendor(
    String similarity,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other: '（別エンティティにほぼ同一の請求書が$crossEntityCount件あります）',
      zero: '',
    );
    return '重複の可能性: 別の請求書と$similarity一致$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarCrossEntity(String similarity) {
    return '重複の可能性: 別エンティティのほぼ同一の請求書と$similarity一致';
  }

  @override
  String invoiceWarningRoundAmount(String amount) {
    return '端数のない金額: $amount';
  }

  @override
  String get invoiceWarningFutureInvoiceDate => '請求日が未来の日付です';

  @override
  String invoiceWarningRushPayment(int days) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days日',
    );
    return '至急支払い: 請求日から$_temp0以内に支払期限が到来します';
  }

  @override
  String get invoiceWarningPastDue => '請求書が支払期限を過ぎています';

  @override
  String get invoiceWarningUnverifiedVendor => '仕入先が未検証です';

  @override
  String invoiceWarningPersonalEmailDomain(String domain) {
    return '仕入先のメールアドレスが個人向けドメインです: $domain';
  }

  @override
  String invoiceWarningNewVendorLargeAmount(int days, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days日',
    );
    return '新規仕入先（$_temp0前に登録）が高額請求 $amount を提出しています';
  }

  @override
  String get invoiceWarningRemitToChanged => 'この仕入先の前回承認済み請求書から送金先住所が変更されています';

  @override
  String invoiceWarningAmountAboveVendorMean(
    String amount,
    String sigma,
    String mean,
  ) {
    return '金額 $amount はこの仕入先の過去平均（$mean）を$sigmaσ上回っています';
  }

  @override
  String invoiceWarningLlmAnomaly(String reason) {
    return 'AI が検知した異常: $reason';
  }

  @override
  String invoiceWarningLineTotalMismatch(
    String lineItemsTotal,
    String headerAmount,
  ) {
    return '明細合計は $lineItemsTotal ですが、請求金額は $headerAmount です';
  }

  @override
  String invoiceWarningPriceVarianceOver(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return '単価が$itemにおけるこの仕入先の基準を$deltaPct上回っています（$unitPrice / 基準 $baselineUnitPrice）';
  }

  @override
  String invoiceWarningPriceVarianceUnder(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return '単価が$itemにおけるこの仕入先の基準を$deltaPct下回っています（$unitPrice / 基準 $baselineUnitPrice）';
  }

  @override
  String invoiceWarningPoNotFound(String poNumber) {
    return '発注 $poNumber が見つかりません';
  }

  @override
  String invoiceWarningPoAmountVariance(
    String variancePct,
    String poNumber,
    String invoiceAmount,
    String poTotal,
  ) {
    return '発注 $poNumber との金額差異 $variancePct（請求 $invoiceAmount / 発注 $poTotal）';
  }

  @override
  String invoiceWarningPoAmountVariancePoCurrencyUnknown(
    String variancePct,
    String poNumber,
    String invoiceAmount,
    String poTotal,
  ) {
    return '通貨が記録されていない発注 $poNumber との金額差異 $variancePct（請求 $invoiceAmount / 発注 $poTotal）';
  }

  @override
  String invoiceWarningPoCurrencyMismatch(
    String invoiceCurrency,
    String poNumber,
    String poCurrency,
  ) {
    return '請求書は $invoiceCurrency、発注 $poNumber は $poCurrency のため、金額を比較していません';
  }

  @override
  String invoiceWarningPoPartialReceipt(String matchType, String poNumber) {
    return '3 ウェイ照合が部分一致 — 発注 $poNumber に対する$matchType照合ですが、発注数量の一部のみ入荷しています';
  }

  @override
  String invoiceWarningPoOverReceipt(
    String receivedQuantity,
    String orderedQuantity,
    String excessQuantity,
    String poNumber,
  ) {
    return '過剰入荷: 発注 $poNumber で発注 $orderedQuantity に対し $receivedQuantity 入荷（+$excessQuantity）';
  }

  @override
  String invoiceWarningPoOverReceiptUnquantified(String poNumber) {
    return '発注 $poNumber の発注数量を超える入荷があります';
  }

  @override
  String invoiceWarningQualityInspectionFailed(String poNumber) {
    return '発注 $poNumber の品質検査が不合格です';
  }

  @override
  String invoiceWarningQualityInspectionFailedNotes(
    String poNumber,
    String notes,
  ) {
    return '発注 $poNumber の品質検査が不合格です: $notes';
  }

  @override
  String invoiceWarningQualityInspectionMissing(String poNumber) {
    return '発注 $poNumber に品質検査が必要ですが記録がありません';
  }

  @override
  String invoiceWarningQualityPartialAcceptance(
    String acceptedQuantity,
    String poNumber,
  ) {
    return '一部受入: 発注 $poNumber で発注数量のうち $acceptedQuantity を受入';
  }

  @override
  String invoiceWarningQualityPartialAcceptanceUnquantified(String poNumber) {
    return '発注 $poNumber の検査で品質を一部受入しました';
  }

  @override
  String invoiceWarningRecurringVarianceOver(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return '金額 $amount は定期テンプレート「$templateName」の予定金額 $expectedAmount を$deltaPct上回っています';
  }

  @override
  String invoiceWarningRecurringVarianceUnder(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return '金額 $amount は定期テンプレート「$templateName」の予定金額 $expectedAmount を$deltaPct下回っています';
  }

  @override
  String invoiceWarningContractExpired(
    String invoiceDate,
    String contractNumber,
    String endDate,
  ) {
    return '請求日 $invoiceDate は契約 $contractNumber の満了日（$endDate）より後です';
  }

  @override
  String invoiceWarningContractNotStarted(
    String invoiceDate,
    String contractNumber,
    String startDate,
  ) {
    return '請求日 $invoiceDate は契約 $contractNumber の開始日（$startDate）より前です';
  }

  @override
  String invoiceWarningContractTerminated(String contractNumber) {
    return '解約済み契約 $contractNumber に対する支出が記録されています';
  }

  @override
  String invoiceWarningContractCancelled(String contractNumber) {
    return 'キャンセル済み契約 $contractNumber に対する支出が記録されています';
  }

  @override
  String invoiceWarningContractVendorMismatch(String contractNumber) {
    return '請求書の仕入先が契約 $contractNumber の仕入先と一致しません';
  }

  @override
  String invoiceWarningContractSpendLimitExceeded(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return '累計支出 $cumulativeSpend が契約 $contractNumber の上限 $spendLimit を超えています';
  }

  @override
  String invoiceWarningContractSpendLimitExceededNotToExceed(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return '累計支出 $cumulativeSpend が契約 $contractNumber の上限 $spendLimit を超えています（超過不可）';
  }

  @override
  String invoiceWarningContractGlNotAllowed(
    String glAccount,
    String contractNumber,
  ) {
    return '勘定科目 $glAccount は契約 $contractNumber で許可された科目の範囲外です';
  }

  @override
  String invoiceWarningSelfCorrectionTotalReconciliation(
    String subtotal,
    String tax,
    String shipping,
    String discount,
    String expected,
    String amount,
  ) {
    return '金額が一致しません: 小計（$subtotal）+ 税（$tax）+ 送料（$shipping）− 割引（$discount）= $expected ですが、合計は $amount です。';
  }

  @override
  String invoiceWarningSelfCorrectionDateOrdering(
    String dueDate,
    String invoiceDate,
  ) {
    return '支払期限（$dueDate）が請求日（$invoiceDate）より前です。';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemsSum(
    String lineItemsTotal,
    String amount,
  ) {
    return '明細合計（$lineItemsTotal）が請求金額（$amount）と一致しません。';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemMath(
    String lineNumber,
    String quantity,
    String unitPrice,
    String expected,
    String total,
  ) {
    return '明細 $lineNumber: $quantity × $unitPrice = $expected ですが、合計は $total です。';
  }

  @override
  String invoiceWarningGlCodesNotInChart(String codes) {
    return 'AI が提案した勘定科目が有効な勘定科目表にありません: $codes';
  }

  @override
  String invoiceWarningGlCodeStalePrior(String code) {
    return 'この仕入先のキャッシュされた勘定科目「$code」は有効な勘定科目表にありません。';
  }
}
