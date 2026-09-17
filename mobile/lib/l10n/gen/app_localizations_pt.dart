// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for Portuguese (`pt`).
class AppLocalizationsPt extends AppLocalizations {
  AppLocalizationsPt([String locale = 'pt']) : super(locale);

  @override
  String get navDashboard => 'Painel';

  @override
  String get navInvoices => 'Faturas';

  @override
  String get navContracts => 'Contratos';

  @override
  String get navApprovals => 'Aprovações';

  @override
  String get navExceptions => 'Exceções';

  @override
  String get navVendors => 'Fornecedores';

  @override
  String get navPay => 'Pagar';

  @override
  String get navPayments => 'Pagamentos';

  @override
  String get navSettings => 'Configurações';

  @override
  String get shellAppName => 'FeohLedger';

  @override
  String get commonSave => 'Salvar';

  @override
  String get commonSaving => 'Salvando…';

  @override
  String get commonCancel => 'Cancelar';

  @override
  String get commonLoading => 'Carregando…';

  @override
  String get commonRetry => 'Tentar novamente';

  @override
  String get commonAll => 'Todas';

  @override
  String get commonSearch => 'Pesquisar';

  @override
  String get commonClear => 'Limpar';

  @override
  String get commonApply => 'Aplicar';

  @override
  String get commonClose => 'Fechar';

  @override
  String get settingsTitle => 'Configurações';

  @override
  String get settingsTenant => 'Organização';

  @override
  String get settingsTenantNotSet => 'Não definido';

  @override
  String get settingsApiServer => 'Servidor da API';

  @override
  String get settingsBiometricUnlock => 'Desbloqueio biométrico';

  @override
  String get settingsBiometricHint =>
      'Usar impressão digital ou rosto para desbloquear';

  @override
  String get settingsSignOut => 'Sair';

  @override
  String get settingsLanguage => 'Idioma';

  @override
  String get settingsLanguageHint =>
      'Escolha o idioma usado em todo o aplicativo. Sua escolha é salva neste dispositivo.';

  @override
  String get settingsLanguageSystem => 'Padrão do sistema';

  @override
  String get dashboardTitle => 'Painel';

  @override
  String get dashboardTotalInvoices => 'Total de faturas';

  @override
  String get dashboardUpcoming => 'A vencer';

  @override
  String get dashboardForReview => 'Para revisão';

  @override
  String get dashboardApproved => 'Aprovadas';

  @override
  String get dashboardAging => 'Idade das faturas';

  @override
  String get dashboardTopVendors => 'Principais fornecedores';

  @override
  String get dashboardAgingCurrent => 'Em dia';

  @override
  String get dashboardAgingDays30 => '30 dias';

  @override
  String get dashboardAgingDays60 => '60 dias';

  @override
  String get dashboardAgingDays90plus => '90+';

  @override
  String get dashboardCachedBanner =>
      'Dados em cache — não foi possível conectar ao servidor';

  @override
  String dashboardErrorPrefix(String error) {
    return 'Erro: $error';
  }

  @override
  String dashboardInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas',
      one: '$count fatura',
    );
    return '$_temp0';
  }

  @override
  String get invoicesTitle => 'Faturas';

  @override
  String get invoicesSearchHint => 'Pesquisar faturas…';

  @override
  String get invoicesSearchAria => 'Pesquisar faturas';

  @override
  String get invoicesAdvancedSearch => 'Pesquisa avançada';

  @override
  String get invoicesAdvancedSearchActive =>
      'Pesquisa avançada, filtros ativos';

  @override
  String get invoicesCaptureInvoice => 'Capturar fatura';

  @override
  String get invoicesCaptureInvoiceLabel => 'Capturar fatura';

  @override
  String get invoicesEmpty => 'Nenhuma fatura encontrada';

  @override
  String get invoicesFilterAll => 'Todas';

  @override
  String get invoicesFilterNew => 'Novas';

  @override
  String get invoicesFilterPending => 'Pendentes';

  @override
  String get invoicesFilterReview => 'Revisão';

  @override
  String get invoicesFilterApproved => 'Aprovadas';

  @override
  String get invoicesFilterRejected => 'Rejeitadas';

  @override
  String get invoicesFilterPaid => 'Pagas';

  @override
  String get invoicesColInvoiceNumber => 'N.º da fatura';

  @override
  String get invoicesColVendor => 'Fornecedor';

  @override
  String get invoicesColAmount => 'Valor';

  @override
  String get invoicesColDueDate => 'Vencimento';

  @override
  String get invoicesColStatus => 'Status';

  @override
  String get notificationsTitle => 'Notificações';

  @override
  String get notificationsMarkAllRead => 'Marcar tudo como lido';

  @override
  String get notificationsMarkAllReadLabel =>
      'Marcar todas as notificações como lidas';

  @override
  String get notificationsFilterUnread => 'Não lidas';

  @override
  String get notificationsAllMarkedRead =>
      'Todas as notificações marcadas como lidas';

  @override
  String get notificationsCouldNotMarkAll =>
      'Não foi possível marcar tudo como lido';

  @override
  String get notificationsEmptyUnread => 'Nenhuma notificação não lida';

  @override
  String get notificationsEmpty => 'Nenhuma notificação';

  @override
  String get notificationsCaughtUp => 'Você está em dia';

  @override
  String get notificationsNothingYet => 'Nada por aqui ainda';

  @override
  String get notificationsLoadError =>
      'Não foi possível carregar as notificações';

  @override
  String get vendorsTitle => 'Fornecedores';

  @override
  String get vendorsSyncErp => 'Sincronizar do ERP';

  @override
  String get vendorsSyncErpLabel => 'Sincronizar fornecedores do ERP';

  @override
  String get vendorsSearchHint => 'Pesquisar fornecedores…';

  @override
  String get vendorsFilterUnverified => 'Não verificados';

  @override
  String get vendorsFilterActive => 'Ativos';

  @override
  String get vendorsFilterInactive => 'Inativos';

  @override
  String get vendorsFilterRejected => 'Rejeitados';

  @override
  String get vendorsEmpty => 'Nenhum fornecedor encontrado';

  @override
  String get vendorsLoadError => 'Não foi possível carregar os fornecedores';

  @override
  String get vendorActionVerify => 'Verificar';

  @override
  String get vendorActionReject => 'Rejeitar';

  @override
  String get vendorUnverifiedLabel => 'Fornecedor não verificado';

  @override
  String get vendorVerifyHint => 'Tornar elegível para pagamento';

  @override
  String get vendorRejectHint => 'Marcar como inválido / duplicado';

  @override
  String get vendorVerified => 'Fornecedor verificado';

  @override
  String get vendorRejected => 'Fornecedor rejeitado';

  @override
  String get vendorActionFailed => 'Falha na ação';

  @override
  String vendorSyncFailed(String error) {
    return 'Falha na sincronização com o ERP: $error';
  }

  @override
  String get exceptionsTitle => 'Exceções';

  @override
  String get exceptionsFilterOpen => 'Abertas';

  @override
  String get exceptionsFilterEscalated => 'Escaladas';

  @override
  String get exceptionsFilterResolved => 'Resolvidas';

  @override
  String get exceptionsFilterDismissed => 'Descartadas';

  @override
  String get exceptionsEmpty => 'Nenhuma exceção';

  @override
  String get exceptionsQueueClear => 'A fila de exceções está vazia';

  @override
  String get exceptionActionResolve => 'Resolver';

  @override
  String get exceptionActionEscalate => 'Escalar';

  @override
  String get exceptionActionDismiss => 'Descartar';

  @override
  String get exceptionResolved => 'Exceção resolvida';

  @override
  String get exceptionEscalated => 'Exceção escalada';

  @override
  String get exceptionDismissed => 'Exceção descartada';

  @override
  String get exceptionActionFailed => 'Falha na ação';

  @override
  String get paymentsTitle => 'Pagamentos';

  @override
  String get paymentsEmpty => 'Nenhum pagamento';

  @override
  String paymentsErrorPrefix(String error) {
    return 'Erro: $error';
  }

  @override
  String get paymentStatusPending => 'Pendente';

  @override
  String get paymentStatusProcessing => 'Em processamento';

  @override
  String get paymentStatusCompleted => 'Concluído';

  @override
  String get paymentStatusFailed => 'Falhou';

  @override
  String get paymentStatusCancelled => 'Cancelado';

  @override
  String get approvalsTitle => 'Aprovações pendentes';

  @override
  String get approvalsAllCaughtUp => 'Tudo em dia!';

  @override
  String get approvalsNoneWaiting => 'Nenhuma fatura aguardando aprovação';

  @override
  String get approvalsLoadError =>
      'Não foi possível carregar as aprovações pendentes';

  @override
  String approvalsPendingCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas pendentes',
      one: '$count fatura pendente',
    );
    return '$_temp0';
  }

  @override
  String get approvalActionApprove => 'Aprovar';

  @override
  String get approvalActionReject => 'Rejeitar';

  @override
  String get approvalApproved => 'Fatura aprovada';

  @override
  String get captureTitle => 'Capturar fatura';

  @override
  String get captureChange => 'Alterar';

  @override
  String get captureUpload => 'Enviar';

  @override
  String get captureUploading => 'Enviando…';

  @override
  String get captureEmptyPrompt =>
      'Tire uma foto, escolha da galeria ou selecione um arquivo';

  @override
  String get captureCamera => 'Câmera';

  @override
  String get captureGallery => 'Galeria';

  @override
  String get captureChooseFile => 'Escolher arquivo';

  @override
  String get captureSupportedFormats => 'Compatível com PDF, PNG, JPG e TIFF';

  @override
  String get captureUploadSuccess => 'Fatura enviada com sucesso';

  @override
  String captureUploadFailedStatus(int status, String message) {
    return 'Falha no envio ($status): $message';
  }

  @override
  String captureUploadFailed(String error) {
    return 'Falha no envio: $error';
  }

  @override
  String captureSelectedDocument(String name) {
    return 'Documento selecionado: $name';
  }

  @override
  String get capturePdfReady => 'Documento PDF pronto para envio';

  @override
  String get advSearchTitle => 'Busca avançada';

  @override
  String get advSearchClose => 'Fechar busca avançada';

  @override
  String get advSearchVendor => 'Fornecedor';

  @override
  String get advSearchPoNumber => 'Número do pedido';

  @override
  String get advSearchMinAmount => 'Valor mínimo';

  @override
  String get advSearchMaxAmount => 'Valor máximo';

  @override
  String get advSearchDueFrom => 'Vencimento de';

  @override
  String get advSearchDueTo => 'Vencimento até';

  @override
  String get advSearchAny => 'Qualquer';

  @override
  String get advSearchInvalidAmount => 'Insira um valor válido (ex.: 1000)';

  @override
  String get advSearchMinMaxError => 'O mínimo não deve exceder o máximo';

  @override
  String advSearchClearField(String label) {
    return 'Limpar $label';
  }

  @override
  String advSearchDateFieldHint(String label, String value) {
    return '$label, atualmente $value. Toque duas vezes para alterar.';
  }

  @override
  String get invoiceDetailTitle => 'Detalhe da fatura';

  @override
  String get invoiceDetailEdit => 'Editar';

  @override
  String get invoiceDetailEditLabel => 'Editar fatura';

  @override
  String get invoiceDetailRetry => 'Tentar novamente';

  @override
  String invoiceDetailErrorPrefix(String error) {
    return 'Erro: $error';
  }

  @override
  String get invoiceDetailNoChanges => 'Nenhuma alteração para salvar';

  @override
  String get invoiceDetailUpdated => 'Fatura atualizada';

  @override
  String get invoiceDetailUpdateFailed =>
      'Não foi possível salvar as alterações — tente novamente';

  @override
  String get invoiceDetailApproved => 'Fatura aprovada';

  @override
  String get invoiceDetailApproveFailed =>
      'Não foi possível aprovar a fatura — tente novamente';

  @override
  String get invoiceDetailRejected => 'Fatura rejeitada';

  @override
  String get invoiceDetailRejectFailed =>
      'Não foi possível rejeitar a fatura — tente novamente';

  @override
  String get invoiceDetailRejectTitle => 'Rejeitar fatura';

  @override
  String get invoiceDetailRejectReason => 'Motivo';

  @override
  String get invoiceDetailReject => 'Rejeitar';

  @override
  String get invoiceDetailApprove => 'Aprovar';

  @override
  String get invoiceDetailUnknownVendor => 'Fornecedor desconhecido';

  @override
  String get invoiceDetailFieldInvoiceNumber => 'N.º da fatura';

  @override
  String get invoiceDetailFieldPoNumber => 'N.º do pedido';

  @override
  String get invoiceDetailFieldCurrency => 'Moeda';

  @override
  String get invoiceDetailFieldInvoiceDate => 'Data da fatura';

  @override
  String get invoiceDetailFieldDueDate => 'Data de vencimento';

  @override
  String get invoiceDetailFieldDescription => 'Descrição';

  @override
  String get invoiceDetailFieldGlAccount => 'Conta contábil';

  @override
  String get invoiceDetailFieldCreated => 'Criada';

  @override
  String get invoiceDetailActivity => 'Atividade';

  @override
  String get invoiceDetailActivityError =>
      'Não foi possível carregar a atividade';

  @override
  String get invoiceDetailFilePdfLabel =>
      'PDF da fatura. Toque duas vezes para ver em tela cheia.';

  @override
  String get invoiceDetailFileLabel =>
      'Arquivo da fatura. Toque duas vezes para ver em tela cheia.';

  @override
  String get invoiceDetailTapToViewPdf => 'Toque para ver o PDF';

  @override
  String get invoiceDetailTapToViewFile => 'Toque para ver o arquivo';

  @override
  String get invoiceEditTitle => 'Editar fatura';

  @override
  String get invoiceEditClose => 'Fechar o formulário de edição';

  @override
  String get invoiceEditVendor => 'Fornecedor';

  @override
  String get invoiceEditInvoiceNumber => 'N.º da fatura';

  @override
  String get invoiceEditAmount => 'Valor';

  @override
  String get invoiceEditPoNumber => 'N.º do pedido';

  @override
  String get invoiceEditGlAccount => 'Conta contábil';

  @override
  String get invoiceEditDescription => 'Descrição';

  @override
  String get invoiceEditDueDate => 'Data de vencimento';

  @override
  String get invoiceEditNotSet => 'Não definida';

  @override
  String get invoiceEditInvalidAmount =>
      'Insira um valor válido (ex.: 1234,56)';

  @override
  String get invoiceEditClearDueDate => 'Limpar data de vencimento';

  @override
  String get invoiceEditLockedNotice =>
      'Aprovada — o beneficiário e o valor estão bloqueados. Para alterá-los, rejeite a fatura, corrija-a e aprove novamente.';

  @override
  String get invoiceEditLockedHelper => 'Bloqueado após a aprovação';

  @override
  String invoiceEditDueDateHint(String value) {
    return 'Data de vencimento, atualmente $value. Toque duas vezes para alterar.';
  }

  @override
  String get warningsSectionTitle => 'Avisos e alertas de fraude';

  @override
  String get warningsPoMatchTitle => 'Conciliação de pedido';

  @override
  String get warningsSeverityError => 'Erro';

  @override
  String get warningsSeverityWarning => 'Aviso';

  @override
  String get warningsSeverityInfo => 'Informação';

  @override
  String get warningsPoLabel => 'Pedido';

  @override
  String warningsMatchLabel(String type) {
    return 'Conciliação $type';
  }

  @override
  String warningsVarianceLabel(String value) {
    return '$value% de variação';
  }

  @override
  String get erpStatusTitle => 'Status do ERP';

  @override
  String get erpStatusReference => 'Referência do ERP';

  @override
  String get erpStatusDocumentId => 'ID do documento';

  @override
  String get erpStatusError => 'Erro';

  @override
  String get erpStatusLastUpdate => 'Última atualização';

  @override
  String get erpStatusStatus => 'Status';

  @override
  String get fileViewerPdfTitle => 'PDF da fatura';

  @override
  String get fileViewerImageTitle => 'Imagem da fatura';

  @override
  String get fileViewerPdfError => 'Não foi possível carregar o PDF';

  @override
  String get fileViewerImageError => 'Não foi possível carregar a imagem';

  @override
  String get fileViewerRetry => 'Tentar novamente';

  @override
  String get timelineNoActivity => 'Nenhuma atividade ainda';

  @override
  String get payTitle => 'Pagar';

  @override
  String get payTabQueue => 'Fila';

  @override
  String get payTabRuns => 'Lotes';

  @override
  String get paySummaryTotalPaid => 'Total pago';

  @override
  String get paySummaryPending => 'Pendente';

  @override
  String get paySummaryInQueue => 'Na fila';

  @override
  String get paySummaryCardRebates => 'Reembolsos de cartão';

  @override
  String paySummaryPaymentsSubtitle(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count pagamentos',
      one: '$count pagamento',
    );
    return '$_temp0';
  }

  @override
  String get payQueueEmpty => 'Nenhuma fatura aguardando pagamento';

  @override
  String get payQueueError => 'Não foi possível carregar a fila de pagamentos';

  @override
  String get payQueueRetry => 'Tentar novamente';

  @override
  String payQueueDue(String date) {
    return 'Vence em $date';
  }

  @override
  String get payQueueNoDueDate => 'Sem data de vencimento';

  @override
  String payQueueDiscount(String amount) {
    return 'desconto $amount';
  }

  @override
  String get payQueueOverdue => 'vencida';

  @override
  String get payQueueSelected => 'selecionada';

  @override
  String get payQueueBlockedDuplicate => 'Possível duplicata — por resolver';

  @override
  String get payQueueBlockedFraudFlag => 'Alerta de fraude — por resolver';

  @override
  String get payQueueBlockedLineTotalMismatch =>
      'Os totais das linhas não conferem — por resolver';

  @override
  String get payQueueBlockedPaymentReconciliation =>
      'Pagamento anterior não conciliado — pode ainda estar em trânsito';

  @override
  String get payQueueBlockedFullyCredited =>
      'Totalmente coberta por notas de crédito — nada a pagar';

  @override
  String get payQueueBlockedLiveVirtualCard =>
      'Um cartão virtual ativo cobre esta fatura — pague-a com cartão';

  @override
  String get payQueueBlockedGeneric =>
      'Uma exceção por resolver bloqueia o pagamento';

  @override
  String payQueueBlockedAnnounce(String reason) {
    return 'não pode ser paga: $reason';
  }

  @override
  String payQueuePinnedMethod(String method) {
    return 'Pagar com $method';
  }

  @override
  String payMethodLabel(String invoiceNumber) {
    return 'Forma de pagamento para $invoiceNumber';
  }

  @override
  String get payMethodAch => 'ACH';

  @override
  String get payMethodWire => 'Transferência';

  @override
  String get payMethodCheck => 'Cheque';

  @override
  String get payMethodVirtualCard => 'Cartão virtual';

  @override
  String paySelectedCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas selecionadas',
      one: '$count fatura selecionada',
    );
    return '$_temp0';
  }

  @override
  String get payClear => 'Limpar';

  @override
  String get payCreateRun => 'Criar lote';

  @override
  String payCreateRunFailed(String error) {
    return 'Falha ao criar o lote: $error';
  }

  @override
  String get payRunsEmpty => 'Nenhum lote de pagamento';

  @override
  String payRunSubtitle(int count, String date) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count pagamentos',
      one: '$count pagamento',
    );
    return '$_temp0 • $date';
  }

  @override
  String get payRunCfoRequiredSuffix => ' • Aprovação do CFO necessária';

  @override
  String payRunAnnounce(String amount, String status, String subtitle) {
    return 'Lote $amount, $status, $subtitle';
  }

  @override
  String get payRunActions => 'Ações do lote';

  @override
  String get payRunActionExecute => 'Executar';

  @override
  String get payRunActionCancel => 'Cancelar';

  @override
  String get payRunActionApprove => 'Aprovar como CFO';

  @override
  String get payRunApproveTitle => 'Aprovar o lote de pagamento?';

  @override
  String payRunApproveBody(String date, int count, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count pagamentos',
      one: '$count pagamento',
    );
    return 'Aprovação do lote criado em $date — $_temp0 num total de $amount. Isto autoriza a execução; não movimenta dinheiro.';
  }

  @override
  String get payRunApproveConfirm => 'Aprovar';

  @override
  String payRunApproveFailed(String error) {
    return 'Falha ao aprovar: $error';
  }

  @override
  String get payRunCfoBlocked =>
      'Este lote precisa da aprovação do CFO antes de poder ser executado.';

  @override
  String get payRunExecuteTitle => 'Executar o lote de pagamento?';

  @override
  String payRunExecuteBody(String amount) {
    return 'Isso envia $amount pelo processador de pagamentos configurado.';
  }

  @override
  String payRunExecuteFailed(String error) {
    return 'Falha ao executar: $error';
  }

  @override
  String payRunCancelFailed(String error) {
    return 'Falha ao cancelar: $error';
  }

  @override
  String get payRunStatusDraft => 'Rascunho';

  @override
  String get payRunStatusCompleted => 'Concluído';

  @override
  String get payRunStatusSubmitted => 'Enviado';

  @override
  String get payRunStatusPartial => 'Parcial';

  @override
  String get payRunStatusFailed => 'Falhou';

  @override
  String get payRunStatusCancelled => 'Cancelado';

  @override
  String get payConfirmCancel => 'Cancelar';

  @override
  String get payConfirmExecute => 'Executar';

  @override
  String get loginAppName => 'FeohLedger';

  @override
  String get loginTagline => 'Contas a pagar, simplificadas';

  @override
  String get loginTenant => 'Inquilino';

  @override
  String get loginEmail => 'E-mail';

  @override
  String get loginPassword => 'Senha';

  @override
  String get loginShowPassword => 'Mostrar senha';

  @override
  String get loginHidePassword => 'Ocultar senha';

  @override
  String get loginRequired => 'Obrigatório';

  @override
  String get loginSignIn => 'Entrar';

  @override
  String get mfaTitle => 'Autenticação de dois fatores';

  @override
  String get mfaHeading => 'Verifique sua identidade';

  @override
  String get mfaPromptEmail =>
      'Digite o código de 6 dígitos que enviamos por e-mail.';

  @override
  String get mfaPromptTotp =>
      'Digite o código de 6 dígitos do seu aplicativo autenticador.';

  @override
  String get mfaEnforcedNotice =>
      'Sua organização exige autenticação de dois fatores. Verifique agora com um código por e-mail e depois conclua a configuração de um aplicativo autenticador no aplicativo web.';

  @override
  String get mfaCode => 'Código';

  @override
  String get mfaCodeRequired => 'Obrigatório';

  @override
  String get mfaCodeTooShort => 'Digite ao menos 6 dígitos';

  @override
  String get mfaVerify => 'Verificar';

  @override
  String get mfaSending => 'Enviando…';

  @override
  String get mfaResendEmailCode => 'Reenviar código por e-mail';

  @override
  String get mfaSendEmailCode => 'Enviar código por e-mail';

  @override
  String get mfaUseEmailInstead => 'Usar um código por e-mail em vez disso';

  @override
  String get mfaUseAuthenticatorInstead =>
      'Usar o aplicativo autenticador em vez disso';

  @override
  String get mfaEmailedAnnounce =>
      'Um código de login foi enviado para o seu e-mail.';

  @override
  String get adminUsersTitle => 'Gerenciamento de usuários';

  @override
  String get adminUsersSearchHint => 'Pesquisar por nome ou e-mail';

  @override
  String get adminUsersEmpty => 'Nenhum usuário encontrado';

  @override
  String get adminUsersLoadError => 'Não foi possível carregar os usuários';

  @override
  String get adminUsersEditRoles => 'Editar funções';

  @override
  String get adminUsersNoRoles => 'Sem funções';

  @override
  String get adminUsersDeactivate => 'Desativar usuário';

  @override
  String get adminUsersActivate => 'Ativar usuário';

  @override
  String get adminUsersCannotDeactivateSelf =>
      'Você não pode desativar sua própria conta';

  @override
  String get adminUsersDeactivateHint =>
      'Desconecta o usuário e bloqueia o login';

  @override
  String get adminUsersActivateHint => 'Restaura o acesso de login';

  @override
  String get adminUsersRoleActive => 'ativo';

  @override
  String get adminUsersRoleInactive => 'inativo';

  @override
  String get adminUsersInactiveBadge => 'Inativo';

  @override
  String adminUsersRolesUpdated(String name) {
    return 'Funções atualizadas para $name';
  }

  @override
  String adminUsersRolesUpdateFailed(String error) {
    return 'Falha ao atualizar as funções: $error';
  }

  @override
  String adminUsersActivated(String name) {
    return '$name ativado';
  }

  @override
  String adminUsersDeactivated(String name) {
    return '$name desativado';
  }

  @override
  String adminUsersUpdateFailed(String name, String error) {
    return 'Falha ao atualizar $name: $error';
  }

  @override
  String get adminUsersCreateUser => 'Criar utilizador';

  @override
  String get adminUsersCreateTitle => 'Novo utilizador';

  @override
  String get adminUsersFieldFullName => 'Nome completo';

  @override
  String get adminUsersFieldEmail => 'E-mail';

  @override
  String get adminUsersFieldRoles => 'Funções';

  @override
  String get adminUsersValidationNameRequired =>
      'O nome completo é obrigatório';

  @override
  String get adminUsersValidationEmailRequired => 'O e-mail é obrigatório';

  @override
  String get adminUsersValidationEmailInvalid =>
      'Introduza um endereço de e-mail válido';

  @override
  String get adminUsersCreateSubmit => 'Criar';

  @override
  String get adminUsersCreating => 'A criar…';

  @override
  String adminUsersCreated(String name) {
    return '$name criado';
  }

  @override
  String adminUsersCreateFailed(String error) {
    return 'Falha ao criar o utilizador: $error';
  }

  @override
  String get adminUsersTempPasswordTitle => 'Utilizador criado';

  @override
  String adminUsersTempPasswordBody(String name) {
    return 'Partilhe esta palavra-passe de uso único com $name. Ser-lhe-á pedido que a altere no primeiro início de sessão. Não será mostrada novamente.';
  }

  @override
  String get adminUsersDelete => 'Eliminar utilizador';

  @override
  String get adminUsersDeleteHint => 'Remove permanentemente esta conta';

  @override
  String get adminUsersCannotDeleteSelf =>
      'Não pode eliminar a sua própria conta';

  @override
  String adminUsersDeleteConfirmTitle(String name) {
    return 'Eliminar $name?';
  }

  @override
  String adminUsersDeleteConfirmBody(String name, String email) {
    return 'Isto remove permanentemente $name ($email). Não pode ser anulado.';
  }

  @override
  String adminUsersDeleted(String name) {
    return '$name eliminado';
  }

  @override
  String adminUsersDeleteFailed(String name, String error) {
    return 'Falha ao eliminar $name: $error';
  }

  @override
  String get orgSettingsTitle => 'Configurações da organização';

  @override
  String get orgSettingsNoSettings => 'Sem configurações';

  @override
  String get orgSettingsLoadError =>
      'Não foi possível carregar as configurações';

  @override
  String get orgSettingsSectionCompany => 'Empresa';

  @override
  String get orgSettingsSectionInvoiceDefaults => 'Padrões de fatura';

  @override
  String get orgSettingsName => 'Nome da organização';

  @override
  String get orgSettingsAddress => 'Endereço';

  @override
  String get orgSettingsPhone => 'Telefone';

  @override
  String get orgSettingsWebsite => 'Site';

  @override
  String get orgSettingsTaxId => 'Identificação fiscal';

  @override
  String get orgSettingsCurrency => 'Moeda padrão';

  @override
  String get orgSettingsPaymentTerms => 'Condições de pagamento';

  @override
  String get orgSettingsNumberPrefix => 'Prefixo do número da fatura';

  @override
  String get orgSettingsGlAccount => 'Conta contábil padrão';

  @override
  String get orgSettingsCostCenter => 'Centro de custo padrão';

  @override
  String get orgSettingsSave => 'Salvar alterações';

  @override
  String get orgSettingsSaving => 'Salvando…';

  @override
  String orgSettingsFieldRequired(String label) {
    return '$label é obrigatório';
  }

  @override
  String get orgSettingsSaved => 'Configurações da organização salvas';

  @override
  String orgSettingsSaveFailed(String error) {
    return 'Falha ao salvar: $error';
  }

  @override
  String get workflowsTitle => 'Fluxos de trabalho';

  @override
  String get workflowsEmpty => 'Nenhum fluxo de trabalho encontrado';

  @override
  String get workflowsLoadError =>
      'Não foi possível carregar os fluxos de trabalho';

  @override
  String get workflowsStatusActive => 'Ativo';

  @override
  String get workflowsStatusInactive => 'Inativo';

  @override
  String get workflowsDefault => 'Padrão';

  @override
  String workflowsStepCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count etapas',
      one: '$count etapa',
    );
    return '$_temp0';
  }

  @override
  String get workflowDetailFallbackTitle => 'Fluxo de trabalho';

  @override
  String get workflowDetailLoadError =>
      'Não foi possível carregar o fluxo de trabalho';

  @override
  String get workflowDetailNoSteps => 'Este fluxo de trabalho não tem etapas.';

  @override
  String get workflowDetailDefaultWorkflow => 'Fluxo de trabalho padrão';

  @override
  String workflowDetailStepNumber(int number) {
    return 'Etapa $number';
  }

  @override
  String get workflowDetailStepEnabled => 'Ativado';

  @override
  String get workflowDetailStepDisabled => 'Desativado';

  @override
  String workflowDetailApproverCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count aprovadores',
      one: '$count aprovador',
    );
    return '$_temp0';
  }

  @override
  String workflowDetailDelaySummary(String hours) {
    return 'Atraso $hours h';
  }

  @override
  String workflowDetailConditionSummary(String field) {
    return 'Em $field';
  }

  @override
  String get cashFlowTitle => 'Previsão de Fluxo de Caixa';

  @override
  String cashFlowErrorPrefix(String error) {
    return 'Erro: $error';
  }

  @override
  String cashFlowHorizonDays(int days) {
    return '$days dias';
  }

  @override
  String get cashFlowLowBalanceAlert => 'Alerta de saldo baixo';

  @override
  String cashFlowBreachSingle(
    String threshold,
    String period,
    String shortfall,
  ) {
    return 'Previsão de queda abaixo do saldo de $threshold em $period (défice de $shortfall).';
  }

  @override
  String cashFlowBreachMultiple(int count, String period, String shortfall) {
    return 'Prevê-se que $count períodos fiquem abaixo do saldo mínimo. Pior caso: $period, défice de $shortfall.';
  }

  @override
  String get cashFlowMinimum => 'mínimo';

  @override
  String get cashFlowOpeningBalance => 'Saldo Inicial';

  @override
  String get cashFlowProjectedEnd => 'Saldo Final Previsto';

  @override
  String cashFlowProjectedEndSubtitle(int days) {
    return 'em $days dias';
  }

  @override
  String get cashFlowCommittedOut => 'Saídas Confirmadas';

  @override
  String get cashFlowCommittedSubtitle => 'compromissos firmes';

  @override
  String get cashFlowPendingOut => 'Saídas Pendentes';

  @override
  String get cashFlowPendingSubtitle => 'pipeline em curso';

  @override
  String get cashFlowOpeningSourceProvider => 'sincronizado do banco';

  @override
  String get cashFlowOpeningSourceSettings => 'saldo guardado';

  @override
  String get cashFlowOpeningSourceQuery => 'manual';

  @override
  String get cashFlowOpeningSourceUnset => 'defina um saldo';

  @override
  String get cashFlowProjectedOutflows => 'Saídas Previstas';

  @override
  String get cashFlowNoOutflows => 'Não há saídas previstas neste horizonte.';

  @override
  String cashFlowInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas',
      one: '$count fatura',
    );
    return '$_temp0';
  }

  @override
  String cashFlowCommittedAmount(String amount) {
    return 'confirmado $amount';
  }

  @override
  String cashFlowPendingAmount(String amount) {
    return 'pendente $amount';
  }

  @override
  String get cashFlowPosition => 'Posição de Caixa';

  @override
  String get cashFlowNoPosition =>
      'Não há projeção de posição de caixa para este horizonte.';

  @override
  String cashFlowOutAmount(String amount) {
    return 'saída $amount';
  }

  @override
  String cashFlowForecastRowLabel(
    String period,
    String scheduled,
    String committed,
    String pending,
    int count,
  ) {
    return '$period: agendado $scheduled, confirmado $committed, pendente $pending, $count faturas';
  }

  @override
  String cashFlowPositionRowLabel(
    String period,
    String opening,
    String outflow,
    String closing,
  ) {
    return '$period: inicial $opening, saída $outflow, final $closing';
  }

  @override
  String get cashFlowBelowThresholdSuffix => ', abaixo do limite';

  @override
  String cashFlowLowBalanceAlertLabel(String message) {
    return 'Alerta de saldo baixo. $message';
  }

  @override
  String get contractsTitle => 'Contratos';

  @override
  String get contractsSearchHint => 'Pesquisar contratos...';

  @override
  String get contractsEmpty => 'Nenhum contrato encontrado';

  @override
  String get contractsFilterDraft => 'Rascunho';

  @override
  String get contractsFilterActive => 'Ativo';

  @override
  String get contractsFilterExpired => 'Expirado';

  @override
  String get contractsFilterTerminated => 'Rescindido';

  @override
  String get contractsFilterCancelled => 'Cancelado';

  @override
  String get contractDetailTitle => 'Detalhe do Contrato';

  @override
  String contractDetailErrorPrefix(String error) {
    return 'Erro: $error';
  }

  @override
  String get contractDetailUntitled => 'Contrato sem título';

  @override
  String get contractDetailFieldContractNumber => 'N.º do Contrato';

  @override
  String get contractDetailFieldVendor => 'Fornecedor';

  @override
  String get contractDetailFieldType => 'Tipo';

  @override
  String get contractDetailFieldCurrency => 'Moeda';

  @override
  String get contractDetailFieldSpendLimit => 'Limite de Despesa';

  @override
  String get contractDetailNotToExceed => ' (não exceder)';

  @override
  String get contractDetailFieldStartDate => 'Data de Início';

  @override
  String get contractDetailFieldEndDate => 'Data de Fim';

  @override
  String get contractDetailFieldSigned => 'Assinado';

  @override
  String get contractDetailFieldAutoRenew => 'Renovação Automática';

  @override
  String get contractDetailYes => 'Sim';

  @override
  String get contractDetailNo => 'Não';

  @override
  String get contractDetailFieldRenewalTerm => 'Período de Renovação';

  @override
  String contractDetailRenewalTermMonths(int months) {
    return '$months meses';
  }

  @override
  String get contractDetailFieldRenewalNotice => 'Aviso de Renovação';

  @override
  String contractDetailRenewalNoticeDays(int days) {
    return '$days dias';
  }

  @override
  String get contractDetailFieldPaymentTerms => 'Condições de Pagamento';

  @override
  String get contractDetailFieldDescription => 'Descrição';

  @override
  String get contractDetailFieldCreated => 'Criado';

  @override
  String get contractDetailSectionSpend => 'Despesa';

  @override
  String get contractDetailSectionLineItems => 'Linhas';

  @override
  String get contractDetailSpendInvoiced => 'Faturado';

  @override
  String contractDetailSpendInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas',
      one: '$count fatura',
    );
    return '$_temp0';
  }

  @override
  String get contractDetailSpendOverLimit => 'Acima do Limite';

  @override
  String get contractDetailSpendRemaining => 'Restante';

  @override
  String contractDetailSpendOfLimit(String limit) {
    return 'de $limit';
  }

  @override
  String get contractDetailSpendNoLimit => 'sem limite definido';

  @override
  String get contractDetailLineItemFallback => 'Linha';

  @override
  String contractDetailLineQty(String quantity) {
    return 'Qtd. $quantity';
  }

  @override
  String contractDetailLineUnitPrice(String price) {
    return '@ $price';
  }

  @override
  String contractDetailLineGl(String account) {
    return 'Conta $account';
  }

  @override
  String get contractActivate => 'Ativar';

  @override
  String get contractActivated => 'Contrato ativado';

  @override
  String get contractActivateFailed =>
      'Não foi possível ativar o contrato — tente novamente';

  @override
  String get contractTerminate => 'Rescindir';

  @override
  String get contractTerminateTitle => 'Rescindir Contrato';

  @override
  String get contractTerminateBody =>
      'Isto termina o contrato antecipadamente. Esta ação não pode ser anulada. Continuar?';

  @override
  String get contractTerminated => 'Contrato rescindido';

  @override
  String get contractTerminateFailed =>
      'Não foi possível rescindir o contrato — tente novamente';

  @override
  String get exceptionDetailTitle => 'Exceção';

  @override
  String get exceptionDetailNotFound => 'Exceção não encontrada';

  @override
  String get exceptionDetailOverdue => 'Em atraso';

  @override
  String get exceptionDetailSectionDescription => 'Descrição';

  @override
  String get exceptionDetailSectionInvoice => 'Fatura';

  @override
  String get exceptionDetailNoLinkedInvoice => 'Nenhuma fatura associada';

  @override
  String get exceptionDetailFieldNumber => 'Número';

  @override
  String get exceptionDetailFieldVendor => 'Fornecedor';

  @override
  String get exceptionDetailFieldAmount => 'Montante';

  @override
  String get exceptionDetailFieldSeverity => 'Gravidade';

  @override
  String get exceptionDetailSectionSla => 'SLA';

  @override
  String get exceptionDetailFieldCreated => 'Criado';

  @override
  String get exceptionDetailFieldDue => 'Prazo';

  @override
  String get exceptionDetailNoSla => 'Sem SLA definido';

  @override
  String get exceptionDetailFieldStatus => 'Estado';

  @override
  String get exceptionDetailOnTrack => 'Dentro do prazo';

  @override
  String get exceptionDetailResolvedIn => 'Resolvido em';

  @override
  String exceptionDetailResolvedInHours(String hours) {
    return '$hours h';
  }

  @override
  String get exceptionDetailSectionAssignee => 'Responsável';

  @override
  String get exceptionDetailUnassigned => 'Sem atribuição';

  @override
  String get exceptionDetailAssign => 'Atribuir';

  @override
  String get exceptionDetailReassign => 'Reatribuir';

  @override
  String get exceptionDetailSectionResolution => 'Resolução';

  @override
  String get exceptionDetailResolutionNote => 'Nota';

  @override
  String get exceptionDetailResolutionBy => 'Por';

  @override
  String get exceptionDetailResolutionAt => 'Em';

  @override
  String get exceptionDetailActionResolved => 'Exceção resolvida';

  @override
  String get exceptionDetailActionEscalated => 'Exceção escalada';

  @override
  String get exceptionDetailActionDismissed => 'Exceção ignorada';

  @override
  String get exceptionDetailActionResolveFailed =>
      'Não foi possível resolver a exceção';

  @override
  String get exceptionDetailActionEscalateFailed =>
      'Não foi possível escalar a exceção';

  @override
  String get exceptionDetailActionDismissFailed =>
      'Não foi possível ignorar a exceção';

  @override
  String get exceptionDetailAssignTo => 'Atribuir a';

  @override
  String get exceptionDetailUnassign => 'Remover atribuição';

  @override
  String exceptionDetailLoadUsersFailed(String error) {
    return 'Não foi possível carregar os utilizadores: $error';
  }

  @override
  String get exceptionDetailAssigneeUpdateFailed =>
      'Não foi possível atualizar o responsável';

  @override
  String get exceptionDetailUnassigned2 => 'Atribuição da exceção removida';

  @override
  String exceptionDetailAssignedTo(String name) {
    return 'Atribuído a $name';
  }

  @override
  String get settingsProcurement => 'Compras';

  @override
  String get settingsInspections => 'Inspeções de qualidade';

  @override
  String get settingsInspectionsHint =>
      'Registrar e consultar inspeções da conciliação de 4 vias';

  @override
  String get settingsAdministration => 'Administração';

  @override
  String get settingsAdminUsers => 'Gestão de usuários';

  @override
  String get settingsAdminUsersHint => 'Papéis, ativar / desativar usuários';

  @override
  String get settingsAdminOrg => 'Configurações da organização';

  @override
  String get settingsAdminOrgHint => 'Perfil da empresa, padrões de fatura';

  @override
  String get settingsAdminWorkflows => 'Fluxos de trabalho';

  @override
  String get settingsAdminWorkflowsHint =>
      'Ver definições de fluxo e suas etapas';

  @override
  String get settingsAdaptive => 'Fluxos adaptativos';

  @override
  String get settingsAdaptiveHint =>
      'Padrões de aprovação, anomalias, sugestões';

  @override
  String get inspectionsTitle => 'Inspeções de qualidade';

  @override
  String get inspectionsRecord => 'Registrar inspeção';

  @override
  String get inspectionsEmpty => 'Nenhuma inspeção de qualidade registrada.';

  @override
  String get inspectionsEmptyFiltered => 'Nenhuma inspeção com este resultado.';

  @override
  String get inspectionsLoadError => 'Falha ao carregar as inspeções';

  @override
  String get inspectionsResultPass => 'Aprovada';

  @override
  String get inspectionsResultFail => 'Reprovada';

  @override
  String get inspectionsResultPartial => 'Aceitação parcial';

  @override
  String get inspectionsResultUnknown => 'Resultado desconhecido';

  @override
  String inspectionsResultAnnounce(String result) {
    return 'Resultado: $result';
  }

  @override
  String get inspectionsNotLinked => 'Sem vínculo';

  @override
  String get inspectionsNotLinkedHint =>
      'Esta inspeção não está vinculada a nenhum recebimento nem pedido de compra, portanto a conciliação nunca a lerá.';

  @override
  String get inspectionsReceiptUnnamed => 'Recebimento sem número';

  @override
  String get inspectionsHintPass =>
      'Mercadoria aceita — a conciliação não muda.';

  @override
  String get inspectionsHintFail =>
      'Mercadoria recusada — a fatura cai para divergência e uma retenção de qualidade bloqueia o pagamento.';

  @override
  String get inspectionsHintPartial =>
      'Parte da mercadoria aceita — a conciliação cai para parcial e a quantidade aceita é sinalizada.';

  @override
  String get inspectionsHintUnknown =>
      'Este resultado está fora do vocabulário aprovada / reprovada / parcial, então não é possível indicar seu efeito na conciliação.';

  @override
  String inspectionsRecorded(String number) {
    return 'Inspeção $number registrada';
  }

  @override
  String inspectionsRecordFailed(String error) {
    return 'Falha ao registrar a inspeção: $error';
  }

  @override
  String inspectionsReceiptsLoadFailed(String error) {
    return 'Falha ao carregar os recebimentos: $error';
  }

  @override
  String get inspectionRecordTitle => 'Registrar inspeção de qualidade';

  @override
  String get inspectionRecordClose => 'Fechar o formulário de inspeção';

  @override
  String get inspectionRecordReceipt => 'Recebimento';

  @override
  String get inspectionRecordReceiptHint =>
      'A conciliação só lê uma inspeção por meio do seu recebimento, então a inspeção precisa indicar a entrega que cobre.';

  @override
  String inspectionRecordReceiptsBounded(int shown, int total) {
    return 'Exibindo os $shown recebimentos mais recentes de $total. Registre pelo aplicativo web se a entrega que você precisa não estiver na lista.';
  }

  @override
  String get inspectionRecordNoReceipts =>
      'Ainda não há recebimentos — uma inspeção cobre uma entrega, então não há nada a registrar.';

  @override
  String get inspectionRecordNumber => 'N.º da inspeção';

  @override
  String get inspectionRecordNumberRequired => 'Informe um n.º de inspeção';

  @override
  String get inspectionRecordResult => 'Resultado';

  @override
  String get inspectionRecordAcceptedQuantity => 'Quantidade aceita';

  @override
  String get inspectionRecordRejectedQuantity => 'Quantidade rejeitada';

  @override
  String get inspectionRecordAcceptedRequired =>
      'Obrigatória em uma aceitação parcial';

  @override
  String get inspectionRecordInvalidQuantity => 'Até 8 dígitos e 4 decimais';

  @override
  String get inspectionRecordInspectedDate => 'Data da inspeção';

  @override
  String get inspectionRecordDateNotSet => 'Não definida';

  @override
  String get inspectionRecordClearDate => 'Limpar a data da inspeção';

  @override
  String get inspectionRecordInspector => 'Inspetor';

  @override
  String get inspectionRecordNotes => 'Observações de desvio';

  @override
  String get inspectionRecordNotesHint =>
      'Citadas literalmente na pendência de conciliação da fatura quando a inspeção é reprovada, para quem tratar a retenção de qualidade.';

  @override
  String get inspectionRecordSubmit => 'Registrar inspeção';

  @override
  String get inspectionDetailTitle => 'Inspeção';

  @override
  String get inspectionDetailNotFound => 'Inspeção não encontrada';

  @override
  String inspectionDetailErrorPrefix(String error) {
    return 'Não foi possível carregar a inspeção: $error';
  }

  @override
  String get inspectionDetailFieldReceipt => 'Recebimento';

  @override
  String get inspectionDetailFieldInspectedDate => 'Inspecionada';

  @override
  String get inspectionDetailFieldInspector => 'Inspetor';

  @override
  String get inspectionDetailFieldAccepted => 'Quantidade aceita';

  @override
  String get inspectionDetailFieldRejected => 'Quantidade rejeitada';

  @override
  String get inspectionDetailFieldStatus => 'Status';

  @override
  String get inspectionDetailFieldCreated => 'Criada';

  @override
  String get inspectionDetailSectionNotes => 'Observações de desvio';

  @override
  String get adaptiveTitle => 'Fluxos adaptativos';

  @override
  String get adaptiveTabSuggestions => 'Sugestões';

  @override
  String get adaptiveTabPatterns => 'Padrões de aprovação';

  @override
  String get adaptiveTabAnomalies => 'Anomalias';

  @override
  String get adaptiveAdvisoryNote =>
      'Tudo aqui é consultivo. Nada nesta tela alterou um fluxo — uma recomendação só passa a valer quando alguém com o papel adequado a aplica no aplicativo web, pelo mesmo caminho auditado de uma edição manual.';

  @override
  String get adaptiveSuggestionsEmpty =>
      'Sem sugestões — ainda não há histórico de aprovação consistente o bastante.';

  @override
  String get adaptiveSuggestionsError =>
      'Não foi possível carregar as sugestões.';

  @override
  String get adaptiveSuggestionsShowOpen => 'Abertas';

  @override
  String get adaptiveSuggestionsShowAll => 'Todas';

  @override
  String adaptiveSuggestionsConfidence(String pct) {
    return 'Confiança $pct%';
  }

  @override
  String get adaptiveSuggestionsDismiss => 'Descartar';

  @override
  String get adaptiveSuggestionsDismissTitle => 'Descartar esta sugestão?';

  @override
  String get adaptiveSuggestionsDismissBody =>
      'Ela continuará descartada após o recálculo, e nenhum fluxo muda de todo modo.';

  @override
  String get adaptiveSuggestionsDismissed => 'Sugestão descartada.';

  @override
  String adaptiveSuggestionsDismissFailed(String error) {
    return 'Não foi possível descartar essa sugestão: $error';
  }

  @override
  String get adaptiveSuggestionStatusOpen => 'Aberta';

  @override
  String get adaptiveSuggestionStatusDismissed => 'Descartada';

  @override
  String get adaptiveSuggestionStatusApplied => 'Aplicada';

  @override
  String get adaptiveSuggestionStatusStale => 'Desatualizada';

  @override
  String get adaptiveSuggestionStatusUnknown => 'Status desconhecido';

  @override
  String adaptiveSuggestionStatusAnnounce(String status) {
    return 'Status: $status';
  }

  @override
  String get adaptivePatternsError =>
      'Não foi possível carregar os padrões de aprovação.';

  @override
  String get adaptivePatternsEmptyApprovers =>
      'Ainda não há decisões de aprovação nesta janela.';

  @override
  String get adaptivePatternsEmptyVendors =>
      'Ainda não há histórico de fornecedores nesta janela.';

  @override
  String get adaptivePatternsSectionApprovers => 'Por aprovador';

  @override
  String get adaptivePatternsSectionVendors => 'Por fornecedor';

  @override
  String adaptivePatternsLookback(int days) {
    return 'Estatísticas determinísticas sobre os últimos $days dias do histórico de aprovação deste inquilino — sem modelo, recalculadas a cada consulta.';
  }

  @override
  String get adaptivePatternsCurrencyNote =>
      'Os valores estão na moeda de relatório da sua organização.';

  @override
  String get adaptivePatternsUnknownApprover => 'Aprovador desconhecido';

  @override
  String adaptivePatternsApproverSummary(int approved, int rejected) {
    return '$approved aprovadas · $rejected rejeitadas';
  }

  @override
  String adaptivePatternsApproverTiming(String rate, String days) {
    return 'Taxa de aprovação $rate% · mediana $days dias';
  }

  @override
  String adaptivePatternsVendorConsistency(String pct) {
    return '$pct% aprovadas sem edição';
  }

  @override
  String adaptivePatternsVendorMoney(String median, String avg) {
    return 'Mediana $median · média $avg';
  }

  @override
  String adaptivePatternsUnconverted(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other:
          '$count aprovações não puderam ser expressas na moeda de relatório e estão excluídas dos valores acima — a amostra ainda as conta',
      one:
          '$count aprovação não pôde ser expressa na moeda de relatório e está excluída dos valores acima — a amostra ainda a conta',
    );
    return '$_temp0';
  }

  @override
  String get adaptiveAnomaliesIntro =>
      'Faturas em revisão que fogem ao padrão estabelecido do próprio fornecedor — no valor, no aprovador ou no tempo de espera. Somente leitura: sinalizar aqui não cria nada e não bloqueia nada.';

  @override
  String get adaptiveAnomaliesEmpty =>
      'Nada em revisão foge ao padrão normal do seu fornecedor.';

  @override
  String get adaptiveAnomaliesError =>
      'Não foi possível carregar a varredura de anomalias.';

  @override
  String adaptiveAnomaliesScanned(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas em revisão analisadas.',
      one: '$count fatura em revisão analisada.',
    );
    return '$_temp0';
  }

  @override
  String adaptiveAnomaliesAmount(String amount, String currency) {
    return '$amount $currency';
  }

  @override
  String get adaptiveAnomaliesInsufficient =>
      'Ainda não há histórico suficiente para este fornecedor.';

  @override
  String get invoiceWarningMissingVendorName => 'Nome do fornecedor ausente';

  @override
  String get invoiceWarningMissingInvoiceNumber => 'Número da nota ausente';

  @override
  String get invoiceWarningMissingAmount => 'Valor ausente ou zero';

  @override
  String get invoiceWarningDuplicateInvoiceNumber =>
      'Número de nota duplicado para este fornecedor';

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
      other:
          ' (mais $crossEntityCount notas quase idênticas em outra entidade)',
      one: ' (mais $crossEntityCount nota quase idêntica em outra entidade)',
      zero: '',
    );
    return 'Possível duplicata: $similarity de correspondência com $invoiceNumber de $vendorName$_temp0';
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
      other:
          ' (mais $crossEntityCount notas quase idênticas em outra entidade)',
      one: ' (mais $crossEntityCount nota quase idêntica em outra entidade)',
      zero: '',
    );
    return 'Possível duplicata: $similarity de correspondência com $invoiceNumber$_temp0';
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
      other:
          ' (mais $crossEntityCount notas quase idênticas em outra entidade)',
      one: ' (mais $crossEntityCount nota quase idêntica em outra entidade)',
      zero: '',
    );
    return 'Possível duplicata: $similarity de correspondência com outra nota de $vendorName$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarUnnumberedUnnamedVendor(
    String similarity,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other:
          ' (mais $crossEntityCount notas quase idênticas em outra entidade)',
      one: ' (mais $crossEntityCount nota quase idêntica em outra entidade)',
      zero: '',
    );
    return 'Possível duplicata: $similarity de correspondência com outra nota$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarCrossEntity(String similarity) {
    return 'Possível duplicata: $similarity de correspondência com uma nota quase idêntica em outra entidade';
  }

  @override
  String invoiceWarningRoundAmount(String amount) {
    return 'Valor redondo: $amount';
  }

  @override
  String get invoiceWarningFutureInvoiceDate => 'A data da nota está no futuro';

  @override
  String invoiceWarningRushPayment(int days) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days dias',
      one: '$days dia',
    );
    return 'Pagamento urgente: vence em $_temp0 a partir da data da nota';
  }

  @override
  String get invoiceWarningPastDue => 'A nota está vencida';

  @override
  String get invoiceWarningUnverifiedVendor =>
      'O fornecedor não está verificado';

  @override
  String invoiceWarningPersonalEmailDomain(String domain) {
    return 'O e-mail do fornecedor usa um domínio pessoal: $domain';
  }

  @override
  String invoiceWarningNewVendorLargeAmount(int days, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days dias',
      one: '$days dia',
    );
    return 'Fornecedor novo (criado há $_temp0) apresentando uma nota alta de $amount';
  }

  @override
  String get invoiceWarningRemitToChanged =>
      'O endereço de pagamento mudou desde a última nota aprovada deste fornecedor';

  @override
  String invoiceWarningAmountAboveVendorMean(
    String amount,
    String sigma,
    String mean,
  ) {
    return 'O valor $amount está $sigmaσ acima da média histórica deste fornecedor ($mean)';
  }

  @override
  String invoiceWarningLlmAnomaly(String reason) {
    return 'Anomalia sinalizada pela IA: $reason';
  }

  @override
  String invoiceWarningLineTotalMismatch(
    String lineItemsTotal,
    String headerAmount,
  ) {
    return 'O total dos itens é $lineItemsTotal, mas o valor da nota é $headerAmount';
  }

  @override
  String invoiceWarningPriceVarianceOver(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Preço unitário $deltaPct acima da referência deste fornecedor para $item ($unitPrice contra $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPriceVarianceUnder(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Preço unitário $deltaPct abaixo da referência deste fornecedor para $item ($unitPrice contra $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPoNotFound(String poNumber) {
    return 'Pedido $poNumber não encontrado';
  }

  @override
  String invoiceWarningPoAmountVariance(
    String variancePct,
    String poNumber,
    String invoiceAmount,
    String poTotal,
  ) {
    return 'Variação de valor $variancePct em relação ao pedido $poNumber (nota $invoiceAmount contra pedido $poTotal)';
  }

  @override
  String invoiceWarningPoPartialReceipt(String matchType, String poNumber) {
    return 'Correspondência parcial de 3 vias — correspondência $matchType com o pedido $poNumber, mas apenas parte da quantidade pedida foi recebida';
  }

  @override
  String invoiceWarningPoOverReceipt(
    String receivedQuantity,
    String orderedQuantity,
    String excessQuantity,
    String poNumber,
  ) {
    return 'Recebimento em excesso: $receivedQuantity recebidas contra $orderedQuantity pedidas (+$excessQuantity) no pedido $poNumber';
  }

  @override
  String invoiceWarningPoOverReceiptUnquantified(String poNumber) {
    return 'Foram recebidas mais mercadorias do que as pedidas no pedido $poNumber';
  }

  @override
  String invoiceWarningQualityInspectionFailed(String poNumber) {
    return 'Inspeção de qualidade reprovada para o pedido $poNumber';
  }

  @override
  String invoiceWarningQualityInspectionFailedNotes(
    String poNumber,
    String notes,
  ) {
    return 'Inspeção de qualidade reprovada para o pedido $poNumber: $notes';
  }

  @override
  String invoiceWarningQualityInspectionMissing(String poNumber) {
    return 'Inspeção de qualidade exigida, mas ausente para o pedido $poNumber';
  }

  @override
  String invoiceWarningQualityPartialAcceptance(
    String acceptedQuantity,
    String poNumber,
  ) {
    return 'Aceitação parcial: $acceptedQuantity da quantidade pedida aceita no pedido $poNumber';
  }

  @override
  String invoiceWarningQualityPartialAcceptanceUnquantified(String poNumber) {
    return 'Aceitação parcial de qualidade na inspeção do pedido $poNumber';
  }

  @override
  String invoiceWarningRecurringVarianceOver(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'O valor $amount está $deltaPct acima do valor previsto $expectedAmount do modelo recorrente “$templateName”';
  }

  @override
  String invoiceWarningRecurringVarianceUnder(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'O valor $amount está $deltaPct abaixo do valor previsto $expectedAmount do modelo recorrente “$templateName”';
  }

  @override
  String invoiceWarningContractExpired(
    String invoiceDate,
    String contractNumber,
    String endDate,
  ) {
    return 'A nota datada de $invoiceDate é posterior ao vencimento do contrato $contractNumber ($endDate)';
  }

  @override
  String invoiceWarningContractNotStarted(
    String invoiceDate,
    String contractNumber,
    String startDate,
  ) {
    return 'A nota datada de $invoiceDate é anterior ao início do contrato $contractNumber ($startDate)';
  }

  @override
  String invoiceWarningContractTerminated(String contractNumber) {
    return 'Gasto registrado no contrato rescindido $contractNumber';
  }

  @override
  String invoiceWarningContractCancelled(String contractNumber) {
    return 'Gasto registrado no contrato cancelado $contractNumber';
  }

  @override
  String invoiceWarningContractVendorMismatch(String contractNumber) {
    return 'O fornecedor da nota não corresponde ao do contrato $contractNumber';
  }

  @override
  String invoiceWarningContractSpendLimitExceeded(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'O gasto acumulado $cumulativeSpend excede o limite $spendLimit do contrato $contractNumber';
  }

  @override
  String invoiceWarningContractSpendLimitExceededNotToExceed(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'O gasto acumulado $cumulativeSpend excede o limite $spendLimit do contrato $contractNumber (não excedível)';
  }

  @override
  String invoiceWarningContractGlNotAllowed(
    String glAccount,
    String contractNumber,
  ) {
    return 'A conta contábil $glAccount está fora das contas permitidas do contrato $contractNumber';
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
    return 'Os valores não fecham: subtotal ($subtotal) + imposto ($tax) + frete ($shipping) − desconto ($discount) = $expected, mas o total é $amount.';
  }

  @override
  String invoiceWarningSelfCorrectionDateOrdering(
    String dueDate,
    String invoiceDate,
  ) {
    return 'A data de vencimento ($dueDate) é anterior à data da nota ($invoiceDate).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemsSum(
    String lineItemsTotal,
    String amount,
  ) {
    return 'O total dos itens ($lineItemsTotal) não corresponde ao valor da nota ($amount).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemMath(
    String lineNumber,
    String quantity,
    String unitPrice,
    String expected,
    String total,
  ) {
    return 'Item $lineNumber: $quantity × $unitPrice = $expected, mas o total é $total.';
  }

  @override
  String invoiceWarningGlCodesNotInChart(String codes) {
    return 'Contas contábeis sugeridas pela IA que não estão no plano ativo: $codes';
  }

  @override
  String invoiceWarningGlCodeStalePrior(String code) {
    return 'A conta contábil “$code” em cache para o fornecedor não está mais no plano de contas ativo.';
  }
}

/// The translations for Portuguese, as used in Brazil (`pt_BR`).
class AppLocalizationsPtBr extends AppLocalizationsPt {
  AppLocalizationsPtBr() : super('pt_BR');

  @override
  String get navDashboard => 'Painel';

  @override
  String get navInvoices => 'Faturas';

  @override
  String get navContracts => 'Contratos';

  @override
  String get navApprovals => 'Aprovações';

  @override
  String get navExceptions => 'Exceções';

  @override
  String get navVendors => 'Fornecedores';

  @override
  String get navPay => 'Pagar';

  @override
  String get navPayments => 'Pagamentos';

  @override
  String get navSettings => 'Configurações';

  @override
  String get shellAppName => 'FeohLedger';

  @override
  String get commonSave => 'Salvar';

  @override
  String get commonSaving => 'Salvando…';

  @override
  String get commonCancel => 'Cancelar';

  @override
  String get commonLoading => 'Carregando…';

  @override
  String get commonRetry => 'Tentar novamente';

  @override
  String get commonAll => 'Todas';

  @override
  String get commonSearch => 'Pesquisar';

  @override
  String get commonClear => 'Limpar';

  @override
  String get commonApply => 'Aplicar';

  @override
  String get commonClose => 'Fechar';

  @override
  String get settingsTitle => 'Configurações';

  @override
  String get settingsTenant => 'Organização';

  @override
  String get settingsTenantNotSet => 'Não definido';

  @override
  String get settingsApiServer => 'Servidor da API';

  @override
  String get settingsBiometricUnlock => 'Desbloqueio biométrico';

  @override
  String get settingsBiometricHint =>
      'Usar impressão digital ou rosto para desbloquear';

  @override
  String get settingsSignOut => 'Sair';

  @override
  String get settingsLanguage => 'Idioma';

  @override
  String get settingsLanguageHint =>
      'Escolha o idioma usado em todo o aplicativo. Sua escolha é salva neste dispositivo.';

  @override
  String get settingsLanguageSystem => 'Padrão do sistema';

  @override
  String get dashboardTitle => 'Painel';

  @override
  String get dashboardTotalInvoices => 'Total de faturas';

  @override
  String get dashboardUpcoming => 'A vencer';

  @override
  String get dashboardForReview => 'Para revisão';

  @override
  String get dashboardApproved => 'Aprovadas';

  @override
  String get dashboardAging => 'Idade das faturas';

  @override
  String get dashboardTopVendors => 'Principais fornecedores';

  @override
  String get dashboardAgingCurrent => 'Em dia';

  @override
  String get dashboardAgingDays30 => '30 dias';

  @override
  String get dashboardAgingDays60 => '60 dias';

  @override
  String get dashboardAgingDays90plus => '90+';

  @override
  String get dashboardCachedBanner =>
      'Dados em cache — não foi possível conectar ao servidor';

  @override
  String dashboardErrorPrefix(String error) {
    return 'Erro: $error';
  }

  @override
  String dashboardInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas',
      one: '$count fatura',
    );
    return '$_temp0';
  }

  @override
  String get invoicesTitle => 'Faturas';

  @override
  String get invoicesSearchHint => 'Pesquisar faturas…';

  @override
  String get invoicesSearchAria => 'Pesquisar faturas';

  @override
  String get invoicesAdvancedSearch => 'Pesquisa avançada';

  @override
  String get invoicesAdvancedSearchActive =>
      'Pesquisa avançada, filtros ativos';

  @override
  String get invoicesCaptureInvoice => 'Capturar fatura';

  @override
  String get invoicesCaptureInvoiceLabel => 'Capturar fatura';

  @override
  String get invoicesEmpty => 'Nenhuma fatura encontrada';

  @override
  String get invoicesFilterAll => 'Todas';

  @override
  String get invoicesFilterNew => 'Novas';

  @override
  String get invoicesFilterPending => 'Pendentes';

  @override
  String get invoicesFilterReview => 'Revisão';

  @override
  String get invoicesFilterApproved => 'Aprovadas';

  @override
  String get invoicesFilterRejected => 'Rejeitadas';

  @override
  String get invoicesFilterPaid => 'Pagas';

  @override
  String get invoicesColInvoiceNumber => 'N.º da fatura';

  @override
  String get invoicesColVendor => 'Fornecedor';

  @override
  String get invoicesColAmount => 'Valor';

  @override
  String get invoicesColDueDate => 'Vencimento';

  @override
  String get invoicesColStatus => 'Status';

  @override
  String get notificationsTitle => 'Notificações';

  @override
  String get notificationsMarkAllRead => 'Marcar tudo como lido';

  @override
  String get notificationsMarkAllReadLabel =>
      'Marcar todas as notificações como lidas';

  @override
  String get notificationsFilterUnread => 'Não lidas';

  @override
  String get notificationsAllMarkedRead =>
      'Todas as notificações marcadas como lidas';

  @override
  String get notificationsCouldNotMarkAll =>
      'Não foi possível marcar tudo como lido';

  @override
  String get notificationsEmptyUnread => 'Nenhuma notificação não lida';

  @override
  String get notificationsEmpty => 'Nenhuma notificação';

  @override
  String get notificationsCaughtUp => 'Você está em dia';

  @override
  String get notificationsNothingYet => 'Nada por aqui ainda';

  @override
  String get notificationsLoadError =>
      'Não foi possível carregar as notificações';

  @override
  String get vendorsTitle => 'Fornecedores';

  @override
  String get vendorsSyncErp => 'Sincronizar do ERP';

  @override
  String get vendorsSyncErpLabel => 'Sincronizar fornecedores do ERP';

  @override
  String get vendorsSearchHint => 'Pesquisar fornecedores…';

  @override
  String get vendorsFilterUnverified => 'Não verificados';

  @override
  String get vendorsFilterActive => 'Ativos';

  @override
  String get vendorsFilterInactive => 'Inativos';

  @override
  String get vendorsFilterRejected => 'Rejeitados';

  @override
  String get vendorsEmpty => 'Nenhum fornecedor encontrado';

  @override
  String get vendorsLoadError => 'Não foi possível carregar os fornecedores';

  @override
  String get vendorActionVerify => 'Verificar';

  @override
  String get vendorActionReject => 'Rejeitar';

  @override
  String get vendorUnverifiedLabel => 'Fornecedor não verificado';

  @override
  String get vendorVerifyHint => 'Tornar elegível para pagamento';

  @override
  String get vendorRejectHint => 'Marcar como inválido / duplicado';

  @override
  String get vendorVerified => 'Fornecedor verificado';

  @override
  String get vendorRejected => 'Fornecedor rejeitado';

  @override
  String get vendorActionFailed => 'Falha na ação';

  @override
  String vendorSyncFailed(String error) {
    return 'Falha na sincronização com o ERP: $error';
  }

  @override
  String get exceptionsTitle => 'Exceções';

  @override
  String get exceptionsFilterOpen => 'Abertas';

  @override
  String get exceptionsFilterEscalated => 'Escaladas';

  @override
  String get exceptionsFilterResolved => 'Resolvidas';

  @override
  String get exceptionsFilterDismissed => 'Descartadas';

  @override
  String get exceptionsEmpty => 'Nenhuma exceção';

  @override
  String get exceptionsQueueClear => 'A fila de exceções está vazia';

  @override
  String get exceptionActionResolve => 'Resolver';

  @override
  String get exceptionActionEscalate => 'Escalar';

  @override
  String get exceptionActionDismiss => 'Descartar';

  @override
  String get exceptionResolved => 'Exceção resolvida';

  @override
  String get exceptionEscalated => 'Exceção escalada';

  @override
  String get exceptionDismissed => 'Exceção descartada';

  @override
  String get exceptionActionFailed => 'Falha na ação';

  @override
  String get paymentsTitle => 'Pagamentos';

  @override
  String get paymentsEmpty => 'Nenhum pagamento';

  @override
  String paymentsErrorPrefix(String error) {
    return 'Erro: $error';
  }

  @override
  String get paymentStatusPending => 'Pendente';

  @override
  String get paymentStatusProcessing => 'Em processamento';

  @override
  String get paymentStatusCompleted => 'Concluído';

  @override
  String get paymentStatusFailed => 'Falhou';

  @override
  String get paymentStatusCancelled => 'Cancelado';

  @override
  String get approvalsTitle => 'Aprovações pendentes';

  @override
  String get approvalsAllCaughtUp => 'Tudo em dia!';

  @override
  String get approvalsNoneWaiting => 'Nenhuma fatura aguardando aprovação';

  @override
  String get approvalsLoadError =>
      'Não foi possível carregar as aprovações pendentes';

  @override
  String approvalsPendingCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas pendentes',
      one: '$count fatura pendente',
    );
    return '$_temp0';
  }

  @override
  String get approvalActionApprove => 'Aprovar';

  @override
  String get approvalActionReject => 'Rejeitar';

  @override
  String get approvalApproved => 'Fatura aprovada';

  @override
  String get captureTitle => 'Capturar fatura';

  @override
  String get captureChange => 'Alterar';

  @override
  String get captureUpload => 'Enviar';

  @override
  String get captureUploading => 'Enviando…';

  @override
  String get captureEmptyPrompt =>
      'Tire uma foto, escolha da galeria ou selecione um arquivo';

  @override
  String get captureCamera => 'Câmera';

  @override
  String get captureGallery => 'Galeria';

  @override
  String get captureChooseFile => 'Escolher arquivo';

  @override
  String get captureSupportedFormats => 'Compatível com PDF, PNG, JPG e TIFF';

  @override
  String get captureUploadSuccess => 'Fatura enviada com sucesso';

  @override
  String captureUploadFailedStatus(int status, String message) {
    return 'Falha no envio ($status): $message';
  }

  @override
  String captureUploadFailed(String error) {
    return 'Falha no envio: $error';
  }

  @override
  String captureSelectedDocument(String name) {
    return 'Documento selecionado: $name';
  }

  @override
  String get capturePdfReady => 'Documento PDF pronto para envio';

  @override
  String get advSearchTitle => 'Busca avançada';

  @override
  String get advSearchClose => 'Fechar busca avançada';

  @override
  String get advSearchVendor => 'Fornecedor';

  @override
  String get advSearchPoNumber => 'Número do pedido';

  @override
  String get advSearchMinAmount => 'Valor mínimo';

  @override
  String get advSearchMaxAmount => 'Valor máximo';

  @override
  String get advSearchDueFrom => 'Vencimento de';

  @override
  String get advSearchDueTo => 'Vencimento até';

  @override
  String get advSearchAny => 'Qualquer';

  @override
  String get advSearchInvalidAmount => 'Insira um valor válido (ex.: 1000)';

  @override
  String get advSearchMinMaxError => 'O mínimo não deve exceder o máximo';

  @override
  String advSearchClearField(String label) {
    return 'Limpar $label';
  }

  @override
  String advSearchDateFieldHint(String label, String value) {
    return '$label, atualmente $value. Toque duas vezes para alterar.';
  }

  @override
  String get invoiceDetailTitle => 'Detalhe da fatura';

  @override
  String get invoiceDetailEdit => 'Editar';

  @override
  String get invoiceDetailEditLabel => 'Editar fatura';

  @override
  String get invoiceDetailRetry => 'Tentar novamente';

  @override
  String invoiceDetailErrorPrefix(String error) {
    return 'Erro: $error';
  }

  @override
  String get invoiceDetailNoChanges => 'Nenhuma alteração para salvar';

  @override
  String get invoiceDetailUpdated => 'Fatura atualizada';

  @override
  String get invoiceDetailUpdateFailed =>
      'Não foi possível salvar as alterações — tente novamente';

  @override
  String get invoiceDetailApproved => 'Fatura aprovada';

  @override
  String get invoiceDetailApproveFailed =>
      'Não foi possível aprovar a fatura — tente novamente';

  @override
  String get invoiceDetailRejected => 'Fatura rejeitada';

  @override
  String get invoiceDetailRejectFailed =>
      'Não foi possível rejeitar a fatura — tente novamente';

  @override
  String get invoiceDetailRejectTitle => 'Rejeitar fatura';

  @override
  String get invoiceDetailRejectReason => 'Motivo';

  @override
  String get invoiceDetailReject => 'Rejeitar';

  @override
  String get invoiceDetailApprove => 'Aprovar';

  @override
  String get invoiceDetailUnknownVendor => 'Fornecedor desconhecido';

  @override
  String get invoiceDetailFieldInvoiceNumber => 'N.º da fatura';

  @override
  String get invoiceDetailFieldPoNumber => 'N.º do pedido';

  @override
  String get invoiceDetailFieldCurrency => 'Moeda';

  @override
  String get invoiceDetailFieldInvoiceDate => 'Data da fatura';

  @override
  String get invoiceDetailFieldDueDate => 'Data de vencimento';

  @override
  String get invoiceDetailFieldDescription => 'Descrição';

  @override
  String get invoiceDetailFieldGlAccount => 'Conta contábil';

  @override
  String get invoiceDetailFieldCreated => 'Criada';

  @override
  String get invoiceDetailActivity => 'Atividade';

  @override
  String get invoiceDetailActivityError =>
      'Não foi possível carregar a atividade';

  @override
  String get invoiceDetailFilePdfLabel =>
      'PDF da fatura. Toque duas vezes para ver em tela cheia.';

  @override
  String get invoiceDetailFileLabel =>
      'Arquivo da fatura. Toque duas vezes para ver em tela cheia.';

  @override
  String get invoiceDetailTapToViewPdf => 'Toque para ver o PDF';

  @override
  String get invoiceDetailTapToViewFile => 'Toque para ver o arquivo';

  @override
  String get invoiceEditTitle => 'Editar fatura';

  @override
  String get invoiceEditClose => 'Fechar o formulário de edição';

  @override
  String get invoiceEditVendor => 'Fornecedor';

  @override
  String get invoiceEditInvoiceNumber => 'N.º da fatura';

  @override
  String get invoiceEditAmount => 'Valor';

  @override
  String get invoiceEditPoNumber => 'N.º do pedido';

  @override
  String get invoiceEditGlAccount => 'Conta contábil';

  @override
  String get invoiceEditDescription => 'Descrição';

  @override
  String get invoiceEditDueDate => 'Data de vencimento';

  @override
  String get invoiceEditNotSet => 'Não definida';

  @override
  String get invoiceEditInvalidAmount =>
      'Insira um valor válido (ex.: 1234,56)';

  @override
  String get invoiceEditClearDueDate => 'Limpar data de vencimento';

  @override
  String get invoiceEditLockedNotice =>
      'Aprovada — o beneficiário e o valor estão bloqueados. Para alterá-los, rejeite a fatura, corrija-a e aprove novamente.';

  @override
  String get invoiceEditLockedHelper => 'Bloqueado após a aprovação';

  @override
  String invoiceEditDueDateHint(String value) {
    return 'Data de vencimento, atualmente $value. Toque duas vezes para alterar.';
  }

  @override
  String get warningsSectionTitle => 'Avisos e alertas de fraude';

  @override
  String get warningsPoMatchTitle => 'Conciliação de pedido';

  @override
  String get warningsSeverityError => 'Erro';

  @override
  String get warningsSeverityWarning => 'Aviso';

  @override
  String get warningsSeverityInfo => 'Informação';

  @override
  String get warningsPoLabel => 'Pedido';

  @override
  String warningsMatchLabel(String type) {
    return 'Conciliação $type';
  }

  @override
  String warningsVarianceLabel(String value) {
    return '$value% de variação';
  }

  @override
  String get erpStatusTitle => 'Status do ERP';

  @override
  String get erpStatusReference => 'Referência do ERP';

  @override
  String get erpStatusDocumentId => 'ID do documento';

  @override
  String get erpStatusError => 'Erro';

  @override
  String get erpStatusLastUpdate => 'Última atualização';

  @override
  String get erpStatusStatus => 'Status';

  @override
  String get fileViewerPdfTitle => 'PDF da fatura';

  @override
  String get fileViewerImageTitle => 'Imagem da fatura';

  @override
  String get fileViewerPdfError => 'Não foi possível carregar o PDF';

  @override
  String get fileViewerImageError => 'Não foi possível carregar a imagem';

  @override
  String get fileViewerRetry => 'Tentar novamente';

  @override
  String get timelineNoActivity => 'Nenhuma atividade ainda';

  @override
  String get payTitle => 'Pagar';

  @override
  String get payTabQueue => 'Fila';

  @override
  String get payTabRuns => 'Lotes';

  @override
  String get paySummaryTotalPaid => 'Total pago';

  @override
  String get paySummaryPending => 'Pendente';

  @override
  String get paySummaryInQueue => 'Na fila';

  @override
  String get paySummaryCardRebates => 'Reembolsos de cartão';

  @override
  String paySummaryPaymentsSubtitle(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count pagamentos',
      one: '$count pagamento',
    );
    return '$_temp0';
  }

  @override
  String get payQueueEmpty => 'Nenhuma fatura aguardando pagamento';

  @override
  String get payQueueError => 'Não foi possível carregar a fila de pagamentos';

  @override
  String get payQueueRetry => 'Tentar novamente';

  @override
  String payQueueDue(String date) {
    return 'Vence em $date';
  }

  @override
  String get payQueueNoDueDate => 'Sem data de vencimento';

  @override
  String payQueueDiscount(String amount) {
    return 'desconto $amount';
  }

  @override
  String get payQueueOverdue => 'vencida';

  @override
  String get payQueueSelected => 'selecionada';

  @override
  String get payQueueBlockedDuplicate => 'Possível duplicata — por resolver';

  @override
  String get payQueueBlockedFraudFlag => 'Alerta de fraude — por resolver';

  @override
  String get payQueueBlockedLineTotalMismatch =>
      'Os totais das linhas não conferem — por resolver';

  @override
  String get payQueueBlockedPaymentReconciliation =>
      'Pagamento anterior não conciliado — pode ainda estar em trânsito';

  @override
  String get payQueueBlockedFullyCredited =>
      'Totalmente coberta por notas de crédito — nada a pagar';

  @override
  String get payQueueBlockedLiveVirtualCard =>
      'Um cartão virtual ativo cobre esta fatura — pague-a com cartão';

  @override
  String get payQueueBlockedGeneric =>
      'Uma exceção por resolver bloqueia o pagamento';

  @override
  String payQueueBlockedAnnounce(String reason) {
    return 'não pode ser paga: $reason';
  }

  @override
  String payQueuePinnedMethod(String method) {
    return 'Pagar com $method';
  }

  @override
  String payMethodLabel(String invoiceNumber) {
    return 'Forma de pagamento para $invoiceNumber';
  }

  @override
  String get payMethodAch => 'ACH';

  @override
  String get payMethodWire => 'Transferência';

  @override
  String get payMethodCheck => 'Cheque';

  @override
  String get payMethodVirtualCard => 'Cartão virtual';

  @override
  String paySelectedCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas selecionadas',
      one: '$count fatura selecionada',
    );
    return '$_temp0';
  }

  @override
  String get payClear => 'Limpar';

  @override
  String get payCreateRun => 'Criar lote';

  @override
  String payCreateRunFailed(String error) {
    return 'Falha ao criar o lote: $error';
  }

  @override
  String get payRunsEmpty => 'Nenhum lote de pagamento';

  @override
  String payRunSubtitle(int count, String date) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count pagamentos',
      one: '$count pagamento',
    );
    return '$_temp0 • $date';
  }

  @override
  String get payRunCfoRequiredSuffix => ' • Aprovação do CFO necessária';

  @override
  String payRunAnnounce(String amount, String status, String subtitle) {
    return 'Lote $amount, $status, $subtitle';
  }

  @override
  String get payRunActions => 'Ações do lote';

  @override
  String get payRunActionExecute => 'Executar';

  @override
  String get payRunActionCancel => 'Cancelar';

  @override
  String get payRunActionApprove => 'Aprovar como CFO';

  @override
  String get payRunApproveTitle => 'Aprovar o lote de pagamento?';

  @override
  String payRunApproveBody(String date, int count, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count pagamentos',
      one: '$count pagamento',
    );
    return 'Aprovação do lote criado em $date — $_temp0 num total de $amount. Isto autoriza a execução; não movimenta dinheiro.';
  }

  @override
  String get payRunApproveConfirm => 'Aprovar';

  @override
  String payRunApproveFailed(String error) {
    return 'Falha ao aprovar: $error';
  }

  @override
  String get payRunCfoBlocked =>
      'Este lote precisa da aprovação do CFO antes de poder ser executado.';

  @override
  String get payRunExecuteTitle => 'Executar o lote de pagamento?';

  @override
  String payRunExecuteBody(String amount) {
    return 'Isso envia $amount pelo processador de pagamentos configurado.';
  }

  @override
  String payRunExecuteFailed(String error) {
    return 'Falha ao executar: $error';
  }

  @override
  String payRunCancelFailed(String error) {
    return 'Falha ao cancelar: $error';
  }

  @override
  String get payRunStatusDraft => 'Rascunho';

  @override
  String get payRunStatusCompleted => 'Concluído';

  @override
  String get payRunStatusSubmitted => 'Enviado';

  @override
  String get payRunStatusPartial => 'Parcial';

  @override
  String get payRunStatusFailed => 'Falhou';

  @override
  String get payRunStatusCancelled => 'Cancelado';

  @override
  String get payConfirmCancel => 'Cancelar';

  @override
  String get payConfirmExecute => 'Executar';

  @override
  String get loginAppName => 'FeohLedger';

  @override
  String get loginTagline => 'Contas a pagar, simplificadas';

  @override
  String get loginTenant => 'Inquilino';

  @override
  String get loginEmail => 'E-mail';

  @override
  String get loginPassword => 'Senha';

  @override
  String get loginShowPassword => 'Mostrar senha';

  @override
  String get loginHidePassword => 'Ocultar senha';

  @override
  String get loginRequired => 'Obrigatório';

  @override
  String get loginSignIn => 'Entrar';

  @override
  String get mfaTitle => 'Autenticação de dois fatores';

  @override
  String get mfaHeading => 'Verifique sua identidade';

  @override
  String get mfaPromptEmail =>
      'Digite o código de 6 dígitos que enviamos por e-mail.';

  @override
  String get mfaPromptTotp =>
      'Digite o código de 6 dígitos do seu aplicativo autenticador.';

  @override
  String get mfaEnforcedNotice =>
      'Sua organização exige autenticação de dois fatores. Verifique agora com um código por e-mail e depois conclua a configuração de um aplicativo autenticador no aplicativo web.';

  @override
  String get mfaCode => 'Código';

  @override
  String get mfaCodeRequired => 'Obrigatório';

  @override
  String get mfaCodeTooShort => 'Digite ao menos 6 dígitos';

  @override
  String get mfaVerify => 'Verificar';

  @override
  String get mfaSending => 'Enviando…';

  @override
  String get mfaResendEmailCode => 'Reenviar código por e-mail';

  @override
  String get mfaSendEmailCode => 'Enviar código por e-mail';

  @override
  String get mfaUseEmailInstead => 'Usar um código por e-mail em vez disso';

  @override
  String get mfaUseAuthenticatorInstead =>
      'Usar o aplicativo autenticador em vez disso';

  @override
  String get mfaEmailedAnnounce =>
      'Um código de login foi enviado para o seu e-mail.';

  @override
  String get adminUsersTitle => 'Gerenciamento de usuários';

  @override
  String get adminUsersSearchHint => 'Pesquisar por nome ou e-mail';

  @override
  String get adminUsersEmpty => 'Nenhum usuário encontrado';

  @override
  String get adminUsersLoadError => 'Não foi possível carregar os usuários';

  @override
  String get adminUsersEditRoles => 'Editar funções';

  @override
  String get adminUsersNoRoles => 'Sem funções';

  @override
  String get adminUsersDeactivate => 'Desativar usuário';

  @override
  String get adminUsersActivate => 'Ativar usuário';

  @override
  String get adminUsersCannotDeactivateSelf =>
      'Você não pode desativar sua própria conta';

  @override
  String get adminUsersDeactivateHint =>
      'Desconecta o usuário e bloqueia o login';

  @override
  String get adminUsersActivateHint => 'Restaura o acesso de login';

  @override
  String get adminUsersRoleActive => 'ativo';

  @override
  String get adminUsersRoleInactive => 'inativo';

  @override
  String get adminUsersInactiveBadge => 'Inativo';

  @override
  String adminUsersRolesUpdated(String name) {
    return 'Funções atualizadas para $name';
  }

  @override
  String adminUsersRolesUpdateFailed(String error) {
    return 'Falha ao atualizar as funções: $error';
  }

  @override
  String adminUsersActivated(String name) {
    return '$name ativado';
  }

  @override
  String adminUsersDeactivated(String name) {
    return '$name desativado';
  }

  @override
  String adminUsersUpdateFailed(String name, String error) {
    return 'Falha ao atualizar $name: $error';
  }

  @override
  String get adminUsersCreateUser => 'Criar usuário';

  @override
  String get adminUsersCreateTitle => 'Novo usuário';

  @override
  String get adminUsersFieldFullName => 'Nome completo';

  @override
  String get adminUsersFieldEmail => 'E-mail';

  @override
  String get adminUsersFieldRoles => 'Funções';

  @override
  String get adminUsersValidationNameRequired =>
      'O nome completo é obrigatório';

  @override
  String get adminUsersValidationEmailRequired => 'O e-mail é obrigatório';

  @override
  String get adminUsersValidationEmailInvalid =>
      'Insira um endereço de e-mail válido';

  @override
  String get adminUsersCreateSubmit => 'Criar';

  @override
  String get adminUsersCreating => 'Criando…';

  @override
  String adminUsersCreated(String name) {
    return '$name criado';
  }

  @override
  String adminUsersCreateFailed(String error) {
    return 'Falha ao criar o usuário: $error';
  }

  @override
  String get adminUsersTempPasswordTitle => 'Usuário criado';

  @override
  String adminUsersTempPasswordBody(String name) {
    return 'Compartilhe esta senha de uso único com $name. Será solicitado que a altere no primeiro login. Ela não será mostrada novamente.';
  }

  @override
  String get adminUsersDelete => 'Excluir usuário';

  @override
  String get adminUsersDeleteHint => 'Remove permanentemente esta conta';

  @override
  String get adminUsersCannotDeleteSelf =>
      'Você não pode excluir sua própria conta';

  @override
  String adminUsersDeleteConfirmTitle(String name) {
    return 'Excluir $name?';
  }

  @override
  String adminUsersDeleteConfirmBody(String name, String email) {
    return 'Isso remove permanentemente $name ($email). Não pode ser desfeito.';
  }

  @override
  String adminUsersDeleted(String name) {
    return '$name excluído';
  }

  @override
  String adminUsersDeleteFailed(String name, String error) {
    return 'Falha ao excluir $name: $error';
  }

  @override
  String get orgSettingsTitle => 'Configurações da organização';

  @override
  String get orgSettingsNoSettings => 'Sem configurações';

  @override
  String get orgSettingsLoadError =>
      'Não foi possível carregar as configurações';

  @override
  String get orgSettingsSectionCompany => 'Empresa';

  @override
  String get orgSettingsSectionInvoiceDefaults => 'Padrões de fatura';

  @override
  String get orgSettingsName => 'Nome da organização';

  @override
  String get orgSettingsAddress => 'Endereço';

  @override
  String get orgSettingsPhone => 'Telefone';

  @override
  String get orgSettingsWebsite => 'Site';

  @override
  String get orgSettingsTaxId => 'Identificação fiscal';

  @override
  String get orgSettingsCurrency => 'Moeda padrão';

  @override
  String get orgSettingsPaymentTerms => 'Condições de pagamento';

  @override
  String get orgSettingsNumberPrefix => 'Prefixo do número da fatura';

  @override
  String get orgSettingsGlAccount => 'Conta contábil padrão';

  @override
  String get orgSettingsCostCenter => 'Centro de custo padrão';

  @override
  String get orgSettingsSave => 'Salvar alterações';

  @override
  String get orgSettingsSaving => 'Salvando…';

  @override
  String orgSettingsFieldRequired(String label) {
    return '$label é obrigatório';
  }

  @override
  String get orgSettingsSaved => 'Configurações da organização salvas';

  @override
  String orgSettingsSaveFailed(String error) {
    return 'Falha ao salvar: $error';
  }

  @override
  String get workflowsTitle => 'Fluxos de trabalho';

  @override
  String get workflowsEmpty => 'Nenhum fluxo de trabalho encontrado';

  @override
  String get workflowsLoadError =>
      'Não foi possível carregar os fluxos de trabalho';

  @override
  String get workflowsStatusActive => 'Ativo';

  @override
  String get workflowsStatusInactive => 'Inativo';

  @override
  String get workflowsDefault => 'Padrão';

  @override
  String workflowsStepCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count etapas',
      one: '$count etapa',
    );
    return '$_temp0';
  }

  @override
  String get workflowDetailFallbackTitle => 'Fluxo de trabalho';

  @override
  String get workflowDetailLoadError =>
      'Não foi possível carregar o fluxo de trabalho';

  @override
  String get workflowDetailNoSteps => 'Este fluxo de trabalho não tem etapas.';

  @override
  String get workflowDetailDefaultWorkflow => 'Fluxo de trabalho padrão';

  @override
  String workflowDetailStepNumber(int number) {
    return 'Etapa $number';
  }

  @override
  String get workflowDetailStepEnabled => 'Ativado';

  @override
  String get workflowDetailStepDisabled => 'Desativado';

  @override
  String workflowDetailApproverCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count aprovadores',
      one: '$count aprovador',
    );
    return '$_temp0';
  }

  @override
  String workflowDetailDelaySummary(String hours) {
    return 'Atraso $hours h';
  }

  @override
  String workflowDetailConditionSummary(String field) {
    return 'Em $field';
  }

  @override
  String get cashFlowTitle => 'Previsão de Fluxo de Caixa';

  @override
  String cashFlowErrorPrefix(String error) {
    return 'Erro: $error';
  }

  @override
  String cashFlowHorizonDays(int days) {
    return '$days dias';
  }

  @override
  String get cashFlowLowBalanceAlert => 'Alerta de saldo baixo';

  @override
  String cashFlowBreachSingle(
    String threshold,
    String period,
    String shortfall,
  ) {
    return 'Previsão de cair abaixo do saldo de $threshold em $period (déficit de $shortfall).';
  }

  @override
  String cashFlowBreachMultiple(int count, String period, String shortfall) {
    return 'Previsão de $count períodos abaixo do saldo mínimo. Pior caso: $period, déficit de $shortfall.';
  }

  @override
  String get cashFlowMinimum => 'mínimo';

  @override
  String get cashFlowOpeningBalance => 'Saldo Inicial';

  @override
  String get cashFlowProjectedEnd => 'Saldo Final Previsto';

  @override
  String cashFlowProjectedEndSubtitle(int days) {
    return 'em $days dias';
  }

  @override
  String get cashFlowCommittedOut => 'Saídas Confirmadas';

  @override
  String get cashFlowCommittedSubtitle => 'compromissos firmes';

  @override
  String get cashFlowPendingOut => 'Saídas Pendentes';

  @override
  String get cashFlowPendingSubtitle => 'pipeline em andamento';

  @override
  String get cashFlowOpeningSourceProvider => 'sincronizado do banco';

  @override
  String get cashFlowOpeningSourceSettings => 'saldo salvo';

  @override
  String get cashFlowOpeningSourceQuery => 'manual';

  @override
  String get cashFlowOpeningSourceUnset => 'defina um saldo';

  @override
  String get cashFlowProjectedOutflows => 'Saídas Previstas';

  @override
  String get cashFlowNoOutflows => 'Não há saídas previstas neste horizonte.';

  @override
  String cashFlowInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas',
      one: '$count fatura',
    );
    return '$_temp0';
  }

  @override
  String cashFlowCommittedAmount(String amount) {
    return 'confirmado $amount';
  }

  @override
  String cashFlowPendingAmount(String amount) {
    return 'pendente $amount';
  }

  @override
  String get cashFlowPosition => 'Posição de Caixa';

  @override
  String get cashFlowNoPosition =>
      'Não há projeção de posição de caixa para este horizonte.';

  @override
  String cashFlowOutAmount(String amount) {
    return 'saída $amount';
  }

  @override
  String cashFlowForecastRowLabel(
    String period,
    String scheduled,
    String committed,
    String pending,
    int count,
  ) {
    return '$period: agendado $scheduled, confirmado $committed, pendente $pending, $count faturas';
  }

  @override
  String cashFlowPositionRowLabel(
    String period,
    String opening,
    String outflow,
    String closing,
  ) {
    return '$period: inicial $opening, saída $outflow, final $closing';
  }

  @override
  String get cashFlowBelowThresholdSuffix => ', abaixo do limite';

  @override
  String cashFlowLowBalanceAlertLabel(String message) {
    return 'Alerta de saldo baixo. $message';
  }

  @override
  String get contractsTitle => 'Contratos';

  @override
  String get contractsSearchHint => 'Pesquisar contratos...';

  @override
  String get contractsEmpty => 'Nenhum contrato encontrado';

  @override
  String get contractsFilterDraft => 'Rascunho';

  @override
  String get contractsFilterActive => 'Ativo';

  @override
  String get contractsFilterExpired => 'Expirado';

  @override
  String get contractsFilterTerminated => 'Rescindido';

  @override
  String get contractsFilterCancelled => 'Cancelado';

  @override
  String get contractDetailTitle => 'Detalhe do Contrato';

  @override
  String contractDetailErrorPrefix(String error) {
    return 'Erro: $error';
  }

  @override
  String get contractDetailUntitled => 'Contrato sem título';

  @override
  String get contractDetailFieldContractNumber => 'Nº do Contrato';

  @override
  String get contractDetailFieldVendor => 'Fornecedor';

  @override
  String get contractDetailFieldType => 'Tipo';

  @override
  String get contractDetailFieldCurrency => 'Moeda';

  @override
  String get contractDetailFieldSpendLimit => 'Limite de Gastos';

  @override
  String get contractDetailNotToExceed => ' (não exceder)';

  @override
  String get contractDetailFieldStartDate => 'Data de Início';

  @override
  String get contractDetailFieldEndDate => 'Data de Término';

  @override
  String get contractDetailFieldSigned => 'Assinado';

  @override
  String get contractDetailFieldAutoRenew => 'Renovação Automática';

  @override
  String get contractDetailYes => 'Sim';

  @override
  String get contractDetailNo => 'Não';

  @override
  String get contractDetailFieldRenewalTerm => 'Período de Renovação';

  @override
  String contractDetailRenewalTermMonths(int months) {
    return '$months meses';
  }

  @override
  String get contractDetailFieldRenewalNotice => 'Aviso de Renovação';

  @override
  String contractDetailRenewalNoticeDays(int days) {
    return '$days dias';
  }

  @override
  String get contractDetailFieldPaymentTerms => 'Condições de Pagamento';

  @override
  String get contractDetailFieldDescription => 'Descrição';

  @override
  String get contractDetailFieldCreated => 'Criado';

  @override
  String get contractDetailSectionSpend => 'Gastos';

  @override
  String get contractDetailSectionLineItems => 'Itens';

  @override
  String get contractDetailSpendInvoiced => 'Faturado';

  @override
  String contractDetailSpendInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas',
      one: '$count fatura',
    );
    return '$_temp0';
  }

  @override
  String get contractDetailSpendOverLimit => 'Acima do Limite';

  @override
  String get contractDetailSpendRemaining => 'Restante';

  @override
  String contractDetailSpendOfLimit(String limit) {
    return 'de $limit';
  }

  @override
  String get contractDetailSpendNoLimit => 'sem limite definido';

  @override
  String get contractDetailLineItemFallback => 'Item';

  @override
  String contractDetailLineQty(String quantity) {
    return 'Qtd. $quantity';
  }

  @override
  String contractDetailLineUnitPrice(String price) {
    return '@ $price';
  }

  @override
  String contractDetailLineGl(String account) {
    return 'Conta $account';
  }

  @override
  String get contractActivate => 'Ativar';

  @override
  String get contractActivated => 'Contrato ativado';

  @override
  String get contractActivateFailed =>
      'Não foi possível ativar o contrato — tente novamente';

  @override
  String get contractTerminate => 'Rescindir';

  @override
  String get contractTerminateTitle => 'Rescindir Contrato';

  @override
  String get contractTerminateBody =>
      'Isto encerra o contrato antecipadamente. Esta ação não pode ser desfeita. Continuar?';

  @override
  String get contractTerminated => 'Contrato rescindido';

  @override
  String get contractTerminateFailed =>
      'Não foi possível rescindir o contrato — tente novamente';

  @override
  String get exceptionDetailTitle => 'Exceção';

  @override
  String get exceptionDetailNotFound => 'Exceção não encontrada';

  @override
  String get exceptionDetailOverdue => 'Em atraso';

  @override
  String get exceptionDetailSectionDescription => 'Descrição';

  @override
  String get exceptionDetailSectionInvoice => 'Fatura';

  @override
  String get exceptionDetailNoLinkedInvoice => 'Nenhuma fatura vinculada';

  @override
  String get exceptionDetailFieldNumber => 'Número';

  @override
  String get exceptionDetailFieldVendor => 'Fornecedor';

  @override
  String get exceptionDetailFieldAmount => 'Valor';

  @override
  String get exceptionDetailFieldSeverity => 'Gravidade';

  @override
  String get exceptionDetailSectionSla => 'SLA';

  @override
  String get exceptionDetailFieldCreated => 'Criado';

  @override
  String get exceptionDetailFieldDue => 'Prazo';

  @override
  String get exceptionDetailNoSla => 'Sem SLA definido';

  @override
  String get exceptionDetailFieldStatus => 'Status';

  @override
  String get exceptionDetailOnTrack => 'Dentro do prazo';

  @override
  String get exceptionDetailResolvedIn => 'Resolvido em';

  @override
  String exceptionDetailResolvedInHours(String hours) {
    return '$hours h';
  }

  @override
  String get exceptionDetailSectionAssignee => 'Responsável';

  @override
  String get exceptionDetailUnassigned => 'Não atribuído';

  @override
  String get exceptionDetailAssign => 'Atribuir';

  @override
  String get exceptionDetailReassign => 'Reatribuir';

  @override
  String get exceptionDetailSectionResolution => 'Resolução';

  @override
  String get exceptionDetailResolutionNote => 'Nota';

  @override
  String get exceptionDetailResolutionBy => 'Por';

  @override
  String get exceptionDetailResolutionAt => 'Em';

  @override
  String get exceptionDetailActionResolved => 'Exceção resolvida';

  @override
  String get exceptionDetailActionEscalated => 'Exceção escalada';

  @override
  String get exceptionDetailActionDismissed => 'Exceção descartada';

  @override
  String get exceptionDetailActionResolveFailed =>
      'Não foi possível resolver a exceção';

  @override
  String get exceptionDetailActionEscalateFailed =>
      'Não foi possível escalar a exceção';

  @override
  String get exceptionDetailActionDismissFailed =>
      'Não foi possível descartar a exceção';

  @override
  String get exceptionDetailAssignTo => 'Atribuir a';

  @override
  String get exceptionDetailUnassign => 'Remover atribuição';

  @override
  String exceptionDetailLoadUsersFailed(String error) {
    return 'Não foi possível carregar os usuários: $error';
  }

  @override
  String get exceptionDetailAssigneeUpdateFailed =>
      'Não foi possível atualizar o responsável';

  @override
  String get exceptionDetailUnassigned2 => 'Atribuição da exceção removida';

  @override
  String exceptionDetailAssignedTo(String name) {
    return 'Atribuído a $name';
  }

  @override
  String get settingsProcurement => 'Compras';

  @override
  String get settingsInspections => 'Inspeções de qualidade';

  @override
  String get settingsInspectionsHint =>
      'Registrar e consultar inspeções da conciliação de 4 vias';

  @override
  String get settingsAdministration => 'Administração';

  @override
  String get settingsAdminUsers => 'Gestão de usuários';

  @override
  String get settingsAdminUsersHint => 'Papéis, ativar / desativar usuários';

  @override
  String get settingsAdminOrg => 'Configurações da organização';

  @override
  String get settingsAdminOrgHint => 'Perfil da empresa, padrões de fatura';

  @override
  String get settingsAdminWorkflows => 'Fluxos de trabalho';

  @override
  String get settingsAdminWorkflowsHint =>
      'Ver definições de fluxo e suas etapas';

  @override
  String get settingsAdaptive => 'Fluxos adaptativos';

  @override
  String get settingsAdaptiveHint =>
      'Padrões de aprovação, anomalias, sugestões';

  @override
  String get inspectionsTitle => 'Inspeções de qualidade';

  @override
  String get inspectionsRecord => 'Registrar inspeção';

  @override
  String get inspectionsEmpty => 'Nenhuma inspeção de qualidade registrada.';

  @override
  String get inspectionsEmptyFiltered => 'Nenhuma inspeção com este resultado.';

  @override
  String get inspectionsLoadError => 'Falha ao carregar as inspeções';

  @override
  String get inspectionsResultPass => 'Aprovada';

  @override
  String get inspectionsResultFail => 'Reprovada';

  @override
  String get inspectionsResultPartial => 'Aceitação parcial';

  @override
  String get inspectionsResultUnknown => 'Resultado desconhecido';

  @override
  String inspectionsResultAnnounce(String result) {
    return 'Resultado: $result';
  }

  @override
  String get inspectionsNotLinked => 'Sem vínculo';

  @override
  String get inspectionsNotLinkedHint =>
      'Esta inspeção não está vinculada a nenhum recebimento nem pedido de compra, portanto a conciliação nunca a lerá.';

  @override
  String get inspectionsReceiptUnnamed => 'Recebimento sem número';

  @override
  String get inspectionsHintPass =>
      'Mercadoria aceita — a conciliação não muda.';

  @override
  String get inspectionsHintFail =>
      'Mercadoria recusada — a fatura cai para divergência e uma retenção de qualidade bloqueia o pagamento.';

  @override
  String get inspectionsHintPartial =>
      'Parte da mercadoria aceita — a conciliação cai para parcial e a quantidade aceita é sinalizada.';

  @override
  String get inspectionsHintUnknown =>
      'Este resultado está fora do vocabulário aprovada / reprovada / parcial, então não é possível indicar seu efeito na conciliação.';

  @override
  String inspectionsRecorded(String number) {
    return 'Inspeção $number registrada';
  }

  @override
  String inspectionsRecordFailed(String error) {
    return 'Falha ao registrar a inspeção: $error';
  }

  @override
  String inspectionsReceiptsLoadFailed(String error) {
    return 'Falha ao carregar os recebimentos: $error';
  }

  @override
  String get inspectionRecordTitle => 'Registrar inspeção de qualidade';

  @override
  String get inspectionRecordClose => 'Fechar o formulário de inspeção';

  @override
  String get inspectionRecordReceipt => 'Recebimento';

  @override
  String get inspectionRecordReceiptHint =>
      'A conciliação só lê uma inspeção por meio do seu recebimento, então a inspeção precisa indicar a entrega que cobre.';

  @override
  String inspectionRecordReceiptsBounded(int shown, int total) {
    return 'Exibindo os $shown recebimentos mais recentes de $total. Registre pelo aplicativo web se a entrega que você precisa não estiver na lista.';
  }

  @override
  String get inspectionRecordNoReceipts =>
      'Ainda não há recebimentos — uma inspeção cobre uma entrega, então não há nada a registrar.';

  @override
  String get inspectionRecordNumber => 'N.º da inspeção';

  @override
  String get inspectionRecordNumberRequired => 'Informe um n.º de inspeção';

  @override
  String get inspectionRecordResult => 'Resultado';

  @override
  String get inspectionRecordAcceptedQuantity => 'Quantidade aceita';

  @override
  String get inspectionRecordRejectedQuantity => 'Quantidade rejeitada';

  @override
  String get inspectionRecordAcceptedRequired =>
      'Obrigatória em uma aceitação parcial';

  @override
  String get inspectionRecordInvalidQuantity => 'Até 8 dígitos e 4 decimais';

  @override
  String get inspectionRecordInspectedDate => 'Data da inspeção';

  @override
  String get inspectionRecordDateNotSet => 'Não definida';

  @override
  String get inspectionRecordClearDate => 'Limpar a data da inspeção';

  @override
  String get inspectionRecordInspector => 'Inspetor';

  @override
  String get inspectionRecordNotes => 'Observações de desvio';

  @override
  String get inspectionRecordNotesHint =>
      'Citadas literalmente na pendência de conciliação da fatura quando a inspeção é reprovada, para quem tratar a retenção de qualidade.';

  @override
  String get inspectionRecordSubmit => 'Registrar inspeção';

  @override
  String get inspectionDetailTitle => 'Inspeção';

  @override
  String get inspectionDetailNotFound => 'Inspeção não encontrada';

  @override
  String inspectionDetailErrorPrefix(String error) {
    return 'Não foi possível carregar a inspeção: $error';
  }

  @override
  String get inspectionDetailFieldReceipt => 'Recebimento';

  @override
  String get inspectionDetailFieldInspectedDate => 'Inspecionada';

  @override
  String get inspectionDetailFieldInspector => 'Inspetor';

  @override
  String get inspectionDetailFieldAccepted => 'Quantidade aceita';

  @override
  String get inspectionDetailFieldRejected => 'Quantidade rejeitada';

  @override
  String get inspectionDetailFieldStatus => 'Status';

  @override
  String get inspectionDetailFieldCreated => 'Criada';

  @override
  String get inspectionDetailSectionNotes => 'Observações de desvio';

  @override
  String get adaptiveTitle => 'Fluxos adaptativos';

  @override
  String get adaptiveTabSuggestions => 'Sugestões';

  @override
  String get adaptiveTabPatterns => 'Padrões de aprovação';

  @override
  String get adaptiveTabAnomalies => 'Anomalias';

  @override
  String get adaptiveAdvisoryNote =>
      'Tudo aqui é consultivo. Nada nesta tela alterou um fluxo — uma recomendação só passa a valer quando alguém com o papel adequado a aplica no aplicativo web, pelo mesmo caminho auditado de uma edição manual.';

  @override
  String get adaptiveSuggestionsEmpty =>
      'Sem sugestões — ainda não há histórico de aprovação consistente o bastante.';

  @override
  String get adaptiveSuggestionsError =>
      'Não foi possível carregar as sugestões.';

  @override
  String get adaptiveSuggestionsShowOpen => 'Abertas';

  @override
  String get adaptiveSuggestionsShowAll => 'Todas';

  @override
  String adaptiveSuggestionsConfidence(String pct) {
    return 'Confiança $pct%';
  }

  @override
  String get adaptiveSuggestionsDismiss => 'Descartar';

  @override
  String get adaptiveSuggestionsDismissTitle => 'Descartar esta sugestão?';

  @override
  String get adaptiveSuggestionsDismissBody =>
      'Ela continuará descartada após o recálculo, e nenhum fluxo muda de todo modo.';

  @override
  String get adaptiveSuggestionsDismissed => 'Sugestão descartada.';

  @override
  String adaptiveSuggestionsDismissFailed(String error) {
    return 'Não foi possível descartar essa sugestão: $error';
  }

  @override
  String get adaptiveSuggestionStatusOpen => 'Aberta';

  @override
  String get adaptiveSuggestionStatusDismissed => 'Descartada';

  @override
  String get adaptiveSuggestionStatusApplied => 'Aplicada';

  @override
  String get adaptiveSuggestionStatusStale => 'Desatualizada';

  @override
  String get adaptiveSuggestionStatusUnknown => 'Status desconhecido';

  @override
  String adaptiveSuggestionStatusAnnounce(String status) {
    return 'Status: $status';
  }

  @override
  String get adaptivePatternsError =>
      'Não foi possível carregar os padrões de aprovação.';

  @override
  String get adaptivePatternsEmptyApprovers =>
      'Ainda não há decisões de aprovação nesta janela.';

  @override
  String get adaptivePatternsEmptyVendors =>
      'Ainda não há histórico de fornecedores nesta janela.';

  @override
  String get adaptivePatternsSectionApprovers => 'Por aprovador';

  @override
  String get adaptivePatternsSectionVendors => 'Por fornecedor';

  @override
  String adaptivePatternsLookback(int days) {
    return 'Estatísticas determinísticas sobre os últimos $days dias do histórico de aprovação deste inquilino — sem modelo, recalculadas a cada consulta.';
  }

  @override
  String get adaptivePatternsCurrencyNote =>
      'Os valores estão na moeda de relatório da sua organização.';

  @override
  String get adaptivePatternsUnknownApprover => 'Aprovador desconhecido';

  @override
  String adaptivePatternsApproverSummary(int approved, int rejected) {
    return '$approved aprovadas · $rejected rejeitadas';
  }

  @override
  String adaptivePatternsApproverTiming(String rate, String days) {
    return 'Taxa de aprovação $rate% · mediana $days dias';
  }

  @override
  String adaptivePatternsVendorConsistency(String pct) {
    return '$pct% aprovadas sem edição';
  }

  @override
  String adaptivePatternsVendorMoney(String median, String avg) {
    return 'Mediana $median · média $avg';
  }

  @override
  String adaptivePatternsUnconverted(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other:
          '$count aprovações não puderam ser expressas na moeda de relatório e estão excluídas dos valores acima — a amostra ainda as conta',
      one:
          '$count aprovação não pôde ser expressa na moeda de relatório e está excluída dos valores acima — a amostra ainda a conta',
    );
    return '$_temp0';
  }

  @override
  String get adaptiveAnomaliesIntro =>
      'Faturas em revisão que fogem ao padrão estabelecido do próprio fornecedor — no valor, no aprovador ou no tempo de espera. Somente leitura: sinalizar aqui não cria nada e não bloqueia nada.';

  @override
  String get adaptiveAnomaliesEmpty =>
      'Nada em revisão foge ao padrão normal do seu fornecedor.';

  @override
  String get adaptiveAnomaliesError =>
      'Não foi possível carregar a varredura de anomalias.';

  @override
  String adaptiveAnomaliesScanned(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count faturas em revisão analisadas.',
      one: '$count fatura em revisão analisada.',
    );
    return '$_temp0';
  }

  @override
  String adaptiveAnomaliesAmount(String amount, String currency) {
    return '$amount $currency';
  }

  @override
  String get adaptiveAnomaliesInsufficient =>
      'Ainda não há histórico suficiente para este fornecedor.';

  @override
  String get invoiceWarningMissingVendorName => 'Nome do fornecedor ausente';

  @override
  String get invoiceWarningMissingInvoiceNumber => 'Número da nota ausente';

  @override
  String get invoiceWarningMissingAmount => 'Valor ausente ou zero';

  @override
  String get invoiceWarningDuplicateInvoiceNumber =>
      'Número de nota duplicado para este fornecedor';

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
      other:
          ' (mais $crossEntityCount notas quase idênticas em outra entidade)',
      one: ' (mais $crossEntityCount nota quase idêntica em outra entidade)',
      zero: '',
    );
    return 'Possível duplicata: $similarity de correspondência com $invoiceNumber de $vendorName$_temp0';
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
      other:
          ' (mais $crossEntityCount notas quase idênticas em outra entidade)',
      one: ' (mais $crossEntityCount nota quase idêntica em outra entidade)',
      zero: '',
    );
    return 'Possível duplicata: $similarity de correspondência com $invoiceNumber$_temp0';
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
      other:
          ' (mais $crossEntityCount notas quase idênticas em outra entidade)',
      one: ' (mais $crossEntityCount nota quase idêntica em outra entidade)',
      zero: '',
    );
    return 'Possível duplicata: $similarity de correspondência com outra nota de $vendorName$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarUnnumberedUnnamedVendor(
    String similarity,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other:
          ' (mais $crossEntityCount notas quase idênticas em outra entidade)',
      one: ' (mais $crossEntityCount nota quase idêntica em outra entidade)',
      zero: '',
    );
    return 'Possível duplicata: $similarity de correspondência com outra nota$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarCrossEntity(String similarity) {
    return 'Possível duplicata: $similarity de correspondência com uma nota quase idêntica em outra entidade';
  }

  @override
  String invoiceWarningRoundAmount(String amount) {
    return 'Valor redondo: $amount';
  }

  @override
  String get invoiceWarningFutureInvoiceDate => 'A data da nota está no futuro';

  @override
  String invoiceWarningRushPayment(int days) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days dias',
      one: '$days dia',
    );
    return 'Pagamento urgente: vence em $_temp0 a partir da data da nota';
  }

  @override
  String get invoiceWarningPastDue => 'A nota está vencida';

  @override
  String get invoiceWarningUnverifiedVendor =>
      'O fornecedor não está verificado';

  @override
  String invoiceWarningPersonalEmailDomain(String domain) {
    return 'O e-mail do fornecedor usa um domínio pessoal: $domain';
  }

  @override
  String invoiceWarningNewVendorLargeAmount(int days, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days dias',
      one: '$days dia',
    );
    return 'Fornecedor novo (criado há $_temp0) apresentando uma nota alta de $amount';
  }

  @override
  String get invoiceWarningRemitToChanged =>
      'O endereço de pagamento mudou desde a última nota aprovada deste fornecedor';

  @override
  String invoiceWarningAmountAboveVendorMean(
    String amount,
    String sigma,
    String mean,
  ) {
    return 'O valor $amount está $sigmaσ acima da média histórica deste fornecedor ($mean)';
  }

  @override
  String invoiceWarningLlmAnomaly(String reason) {
    return 'Anomalia sinalizada pela IA: $reason';
  }

  @override
  String invoiceWarningLineTotalMismatch(
    String lineItemsTotal,
    String headerAmount,
  ) {
    return 'O total dos itens é $lineItemsTotal, mas o valor da nota é $headerAmount';
  }

  @override
  String invoiceWarningPriceVarianceOver(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Preço unitário $deltaPct acima da referência deste fornecedor para $item ($unitPrice contra $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPriceVarianceUnder(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Preço unitário $deltaPct abaixo da referência deste fornecedor para $item ($unitPrice contra $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPoNotFound(String poNumber) {
    return 'Pedido $poNumber não encontrado';
  }

  @override
  String invoiceWarningPoAmountVariance(
    String variancePct,
    String poNumber,
    String invoiceAmount,
    String poTotal,
  ) {
    return 'Variação de valor $variancePct em relação ao pedido $poNumber (nota $invoiceAmount contra pedido $poTotal)';
  }

  @override
  String invoiceWarningPoPartialReceipt(String matchType, String poNumber) {
    return 'Correspondência parcial de 3 vias — correspondência $matchType com o pedido $poNumber, mas apenas parte da quantidade pedida foi recebida';
  }

  @override
  String invoiceWarningPoOverReceipt(
    String receivedQuantity,
    String orderedQuantity,
    String excessQuantity,
    String poNumber,
  ) {
    return 'Recebimento em excesso: $receivedQuantity recebidas contra $orderedQuantity pedidas (+$excessQuantity) no pedido $poNumber';
  }

  @override
  String invoiceWarningPoOverReceiptUnquantified(String poNumber) {
    return 'Foram recebidas mais mercadorias do que as pedidas no pedido $poNumber';
  }

  @override
  String invoiceWarningQualityInspectionFailed(String poNumber) {
    return 'Inspeção de qualidade reprovada para o pedido $poNumber';
  }

  @override
  String invoiceWarningQualityInspectionFailedNotes(
    String poNumber,
    String notes,
  ) {
    return 'Inspeção de qualidade reprovada para o pedido $poNumber: $notes';
  }

  @override
  String invoiceWarningQualityInspectionMissing(String poNumber) {
    return 'Inspeção de qualidade exigida, mas ausente para o pedido $poNumber';
  }

  @override
  String invoiceWarningQualityPartialAcceptance(
    String acceptedQuantity,
    String poNumber,
  ) {
    return 'Aceitação parcial: $acceptedQuantity da quantidade pedida aceita no pedido $poNumber';
  }

  @override
  String invoiceWarningQualityPartialAcceptanceUnquantified(String poNumber) {
    return 'Aceitação parcial de qualidade na inspeção do pedido $poNumber';
  }

  @override
  String invoiceWarningRecurringVarianceOver(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'O valor $amount está $deltaPct acima do valor previsto $expectedAmount do modelo recorrente “$templateName”';
  }

  @override
  String invoiceWarningRecurringVarianceUnder(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'O valor $amount está $deltaPct abaixo do valor previsto $expectedAmount do modelo recorrente “$templateName”';
  }

  @override
  String invoiceWarningContractExpired(
    String invoiceDate,
    String contractNumber,
    String endDate,
  ) {
    return 'A nota datada de $invoiceDate é posterior ao vencimento do contrato $contractNumber ($endDate)';
  }

  @override
  String invoiceWarningContractNotStarted(
    String invoiceDate,
    String contractNumber,
    String startDate,
  ) {
    return 'A nota datada de $invoiceDate é anterior ao início do contrato $contractNumber ($startDate)';
  }

  @override
  String invoiceWarningContractTerminated(String contractNumber) {
    return 'Gasto registrado no contrato rescindido $contractNumber';
  }

  @override
  String invoiceWarningContractCancelled(String contractNumber) {
    return 'Gasto registrado no contrato cancelado $contractNumber';
  }

  @override
  String invoiceWarningContractVendorMismatch(String contractNumber) {
    return 'O fornecedor da nota não corresponde ao do contrato $contractNumber';
  }

  @override
  String invoiceWarningContractSpendLimitExceeded(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'O gasto acumulado $cumulativeSpend excede o limite $spendLimit do contrato $contractNumber';
  }

  @override
  String invoiceWarningContractSpendLimitExceededNotToExceed(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'O gasto acumulado $cumulativeSpend excede o limite $spendLimit do contrato $contractNumber (não excedível)';
  }

  @override
  String invoiceWarningContractGlNotAllowed(
    String glAccount,
    String contractNumber,
  ) {
    return 'A conta contábil $glAccount está fora das contas permitidas do contrato $contractNumber';
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
    return 'Os valores não fecham: subtotal ($subtotal) + imposto ($tax) + frete ($shipping) − desconto ($discount) = $expected, mas o total é $amount.';
  }

  @override
  String invoiceWarningSelfCorrectionDateOrdering(
    String dueDate,
    String invoiceDate,
  ) {
    return 'A data de vencimento ($dueDate) é anterior à data da nota ($invoiceDate).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemsSum(
    String lineItemsTotal,
    String amount,
  ) {
    return 'O total dos itens ($lineItemsTotal) não corresponde ao valor da nota ($amount).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemMath(
    String lineNumber,
    String quantity,
    String unitPrice,
    String expected,
    String total,
  ) {
    return 'Item $lineNumber: $quantity × $unitPrice = $expected, mas o total é $total.';
  }

  @override
  String invoiceWarningGlCodesNotInChart(String codes) {
    return 'Contas contábeis sugeridas pela IA que não estão no plano ativo: $codes';
  }

  @override
  String invoiceWarningGlCodeStalePrior(String code) {
    return 'A conta contábil “$code” em cache para o fornecedor não está mais no plano de contas ativo.';
  }
}
