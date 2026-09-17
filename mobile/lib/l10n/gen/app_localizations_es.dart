// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for Spanish Castilian (`es`).
class AppLocalizationsEs extends AppLocalizations {
  AppLocalizationsEs([String locale = 'es']) : super(locale);

  @override
  String get navDashboard => 'Panel';

  @override
  String get navInvoices => 'Facturas';

  @override
  String get navContracts => 'Contratos';

  @override
  String get navApprovals => 'Aprobaciones';

  @override
  String get navExceptions => 'Excepciones';

  @override
  String get navVendors => 'Proveedores';

  @override
  String get navPay => 'Pagar';

  @override
  String get navPayments => 'Pagos';

  @override
  String get navSettings => 'Ajustes';

  @override
  String get shellAppName => 'FeohLedger';

  @override
  String get commonSave => 'Guardar';

  @override
  String get commonSaving => 'Guardando…';

  @override
  String get commonCancel => 'Cancelar';

  @override
  String get commonLoading => 'Cargando…';

  @override
  String get commonRetry => 'Reintentar';

  @override
  String get commonAll => 'Todas';

  @override
  String get commonSearch => 'Buscar';

  @override
  String get commonClear => 'Limpiar';

  @override
  String get commonApply => 'Aplicar';

  @override
  String get commonClose => 'Cerrar';

  @override
  String get settingsTitle => 'Ajustes';

  @override
  String get settingsTenant => 'Organización';

  @override
  String get settingsTenantNotSet => 'Sin definir';

  @override
  String get settingsApiServer => 'Servidor API';

  @override
  String get settingsBiometricUnlock => 'Desbloqueo biométrico';

  @override
  String get settingsBiometricHint => 'Usar huella o rostro para desbloquear';

  @override
  String get settingsSignOut => 'Cerrar sesión';

  @override
  String get settingsLanguage => 'Idioma';

  @override
  String get settingsLanguageHint =>
      'Elija el idioma utilizado en toda la aplicación. Su elección se guarda en este dispositivo.';

  @override
  String get settingsLanguageSystem => 'Predeterminado del sistema';

  @override
  String get dashboardTitle => 'Panel';

  @override
  String get dashboardTotalInvoices => 'Total de facturas';

  @override
  String get dashboardUpcoming => 'Próximas';

  @override
  String get dashboardForReview => 'Para revisar';

  @override
  String get dashboardApproved => 'Aprobadas';

  @override
  String get dashboardAging => 'Antigüedad de facturas';

  @override
  String get dashboardTopVendors => 'Principales proveedores';

  @override
  String get dashboardAgingCurrent => 'Al día';

  @override
  String get dashboardAgingDays30 => '30 días';

  @override
  String get dashboardAgingDays60 => '60 días';

  @override
  String get dashboardAgingDays90plus => '90+';

  @override
  String get dashboardCachedBanner =>
      'Datos en caché — no se pudo conectar al servidor';

  @override
  String dashboardErrorPrefix(String error) {
    return 'Error: $error';
  }

  @override
  String dashboardInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count facturas',
      one: '$count factura',
    );
    return '$_temp0';
  }

  @override
  String get invoicesTitle => 'Facturas';

  @override
  String get invoicesSearchHint => 'Buscar facturas…';

  @override
  String get invoicesSearchAria => 'Buscar facturas';

  @override
  String get invoicesAdvancedSearch => 'Búsqueda avanzada';

  @override
  String get invoicesAdvancedSearchActive =>
      'Búsqueda avanzada, filtros activos';

  @override
  String get invoicesCaptureInvoice => 'Capturar factura';

  @override
  String get invoicesCaptureInvoiceLabel => 'Capturar factura';

  @override
  String get invoicesEmpty => 'No se encontraron facturas';

  @override
  String get invoicesFilterAll => 'Todas';

  @override
  String get invoicesFilterNew => 'Nuevas';

  @override
  String get invoicesFilterPending => 'Pendientes';

  @override
  String get invoicesFilterReview => 'Revisión';

  @override
  String get invoicesFilterApproved => 'Aprobadas';

  @override
  String get invoicesFilterRejected => 'Rechazadas';

  @override
  String get invoicesFilterPaid => 'Pagadas';

  @override
  String get invoicesColInvoiceNumber => 'N.º de factura';

  @override
  String get invoicesColVendor => 'Proveedor';

  @override
  String get invoicesColAmount => 'Importe';

  @override
  String get invoicesColDueDate => 'Vencimiento';

  @override
  String get invoicesColStatus => 'Estado';

  @override
  String get notificationsTitle => 'Notificaciones';

  @override
  String get notificationsMarkAllRead => 'Marcar todo como leído';

  @override
  String get notificationsMarkAllReadLabel =>
      'Marcar todas las notificaciones como leídas';

  @override
  String get notificationsFilterUnread => 'No leídas';

  @override
  String get notificationsAllMarkedRead =>
      'Todas las notificaciones marcadas como leídas';

  @override
  String get notificationsCouldNotMarkAll =>
      'No se pudo marcar todo como leído';

  @override
  String get notificationsEmptyUnread => 'No hay notificaciones sin leer';

  @override
  String get notificationsEmpty => 'No hay notificaciones';

  @override
  String get notificationsCaughtUp => 'Estás al día';

  @override
  String get notificationsNothingYet => 'Aún no hay nada aquí';

  @override
  String get notificationsLoadError =>
      'No se pudieron cargar las notificaciones';

  @override
  String get vendorsTitle => 'Proveedores';

  @override
  String get vendorsSyncErp => 'Sincronizar desde ERP';

  @override
  String get vendorsSyncErpLabel => 'Sincronizar proveedores desde ERP';

  @override
  String get vendorsSearchHint => 'Buscar proveedores…';

  @override
  String get vendorsFilterUnverified => 'Sin verificar';

  @override
  String get vendorsFilterActive => 'Activos';

  @override
  String get vendorsFilterInactive => 'Inactivos';

  @override
  String get vendorsFilterRejected => 'Rechazados';

  @override
  String get vendorsEmpty => 'No se encontraron proveedores';

  @override
  String get vendorsLoadError => 'No se pudieron cargar los proveedores';

  @override
  String get vendorActionVerify => 'Verificar';

  @override
  String get vendorActionReject => 'Rechazar';

  @override
  String get vendorUnverifiedLabel => 'Proveedor sin verificar';

  @override
  String get vendorVerifyHint => 'Habilitar para pago';

  @override
  String get vendorRejectHint => 'Marcar como no válido / duplicado';

  @override
  String get vendorVerified => 'Proveedor verificado';

  @override
  String get vendorRejected => 'Proveedor rechazado';

  @override
  String get vendorActionFailed => 'Acción fallida';

  @override
  String vendorSyncFailed(String error) {
    return 'Error de sincronización con ERP: $error';
  }

  @override
  String get exceptionsTitle => 'Excepciones';

  @override
  String get exceptionsFilterOpen => 'Abiertas';

  @override
  String get exceptionsFilterEscalated => 'Escaladas';

  @override
  String get exceptionsFilterResolved => 'Resueltas';

  @override
  String get exceptionsFilterDismissed => 'Descartadas';

  @override
  String get exceptionsEmpty => 'No hay excepciones';

  @override
  String get exceptionsQueueClear => 'La cola de excepciones está vacía';

  @override
  String get exceptionActionResolve => 'Resolver';

  @override
  String get exceptionActionEscalate => 'Escalar';

  @override
  String get exceptionActionDismiss => 'Descartar';

  @override
  String get exceptionResolved => 'Excepción resuelta';

  @override
  String get exceptionEscalated => 'Excepción escalada';

  @override
  String get exceptionDismissed => 'Excepción descartada';

  @override
  String get exceptionActionFailed => 'Acción fallida';

  @override
  String get paymentsTitle => 'Pagos';

  @override
  String get paymentsEmpty => 'No hay pagos';

  @override
  String paymentsErrorPrefix(String error) {
    return 'Error: $error';
  }

  @override
  String get paymentStatusPending => 'Pendiente';

  @override
  String get paymentStatusProcessing => 'En proceso';

  @override
  String get paymentStatusCompleted => 'Completado';

  @override
  String get paymentStatusFailed => 'Fallido';

  @override
  String get paymentStatusCancelled => 'Cancelado';

  @override
  String get approvalsTitle => 'Aprobaciones pendientes';

  @override
  String get approvalsAllCaughtUp => '¡Todo al día!';

  @override
  String get approvalsNoneWaiting => 'No hay facturas esperando aprobación';

  @override
  String get approvalsLoadError =>
      'No se pudieron cargar las aprobaciones pendientes';

  @override
  String approvalsPendingCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count facturas pendientes',
      one: '$count factura pendiente',
    );
    return '$_temp0';
  }

  @override
  String get approvalActionApprove => 'Aprobar';

  @override
  String get approvalActionReject => 'Rechazar';

  @override
  String get approvalApproved => 'Factura aprobada';

  @override
  String get captureTitle => 'Capturar factura';

  @override
  String get captureChange => 'Cambiar';

  @override
  String get captureUpload => 'Subir';

  @override
  String get captureUploading => 'Subiendo…';

  @override
  String get captureEmptyPrompt =>
      'Toma una foto, elige de la galería o selecciona un archivo';

  @override
  String get captureCamera => 'Cámara';

  @override
  String get captureGallery => 'Galería';

  @override
  String get captureChooseFile => 'Elegir archivo';

  @override
  String get captureSupportedFormats => 'Admite PDF, PNG, JPG y TIFF';

  @override
  String get captureUploadSuccess => 'Factura subida correctamente';

  @override
  String captureUploadFailedStatus(int status, String message) {
    return 'Error al subir ($status): $message';
  }

  @override
  String captureUploadFailed(String error) {
    return 'Error al subir: $error';
  }

  @override
  String captureSelectedDocument(String name) {
    return 'Documento seleccionado: $name';
  }

  @override
  String get capturePdfReady => 'Documento PDF listo para subir';

  @override
  String get advSearchTitle => 'Búsqueda avanzada';

  @override
  String get advSearchClose => 'Cerrar búsqueda avanzada';

  @override
  String get advSearchVendor => 'Proveedor';

  @override
  String get advSearchPoNumber => 'N.º de pedido';

  @override
  String get advSearchMinAmount => 'Importe mínimo';

  @override
  String get advSearchMaxAmount => 'Importe máximo';

  @override
  String get advSearchDueFrom => 'Vence desde';

  @override
  String get advSearchDueTo => 'Vence hasta';

  @override
  String get advSearchAny => 'Cualquiera';

  @override
  String get advSearchInvalidAmount =>
      'Introduce un importe válido (p. ej. 1000)';

  @override
  String get advSearchMinMaxError => 'El mínimo no debe superar el máximo';

  @override
  String advSearchClearField(String label) {
    return 'Borrar $label';
  }

  @override
  String advSearchDateFieldHint(String label, String value) {
    return '$label, actualmente $value. Toca dos veces para cambiar.';
  }

  @override
  String get invoiceDetailTitle => 'Detalle de la factura';

  @override
  String get invoiceDetailEdit => 'Editar';

  @override
  String get invoiceDetailEditLabel => 'Editar factura';

  @override
  String get invoiceDetailRetry => 'Reintentar';

  @override
  String invoiceDetailErrorPrefix(String error) {
    return 'Error: $error';
  }

  @override
  String get invoiceDetailNoChanges => 'No hay cambios que guardar';

  @override
  String get invoiceDetailUpdated => 'Factura actualizada';

  @override
  String get invoiceDetailUpdateFailed =>
      'No se pudieron guardar los cambios — inténtalo de nuevo';

  @override
  String get invoiceDetailApproved => 'Factura aprobada';

  @override
  String get invoiceDetailApproveFailed =>
      'No se pudo aprobar la factura — inténtalo de nuevo';

  @override
  String get invoiceDetailRejected => 'Factura rechazada';

  @override
  String get invoiceDetailRejectFailed =>
      'No se pudo rechazar la factura — inténtalo de nuevo';

  @override
  String get invoiceDetailRejectTitle => 'Rechazar factura';

  @override
  String get invoiceDetailRejectReason => 'Motivo';

  @override
  String get invoiceDetailReject => 'Rechazar';

  @override
  String get invoiceDetailApprove => 'Aprobar';

  @override
  String get invoiceDetailUnknownVendor => 'Proveedor desconocido';

  @override
  String get invoiceDetailFieldInvoiceNumber => 'N.º de factura';

  @override
  String get invoiceDetailFieldPoNumber => 'N.º de pedido';

  @override
  String get invoiceDetailFieldCurrency => 'Moneda';

  @override
  String get invoiceDetailFieldInvoiceDate => 'Fecha de factura';

  @override
  String get invoiceDetailFieldDueDate => 'Fecha de vencimiento';

  @override
  String get invoiceDetailFieldDescription => 'Descripción';

  @override
  String get invoiceDetailFieldGlAccount => 'Cuenta contable';

  @override
  String get invoiceDetailFieldCreated => 'Creada';

  @override
  String get invoiceDetailActivity => 'Actividad';

  @override
  String get invoiceDetailActivityError => 'No se pudo cargar la actividad';

  @override
  String get invoiceDetailFilePdfLabel =>
      'PDF de la factura. Toca dos veces para ver a pantalla completa.';

  @override
  String get invoiceDetailFileLabel =>
      'Archivo de la factura. Toca dos veces para ver a pantalla completa.';

  @override
  String get invoiceDetailTapToViewPdf => 'Toca para ver el PDF';

  @override
  String get invoiceDetailTapToViewFile => 'Toca para ver el archivo';

  @override
  String get invoiceEditTitle => 'Editar factura';

  @override
  String get invoiceEditClose => 'Cerrar el formulario de edición';

  @override
  String get invoiceEditVendor => 'Proveedor';

  @override
  String get invoiceEditInvoiceNumber => 'N.º de factura';

  @override
  String get invoiceEditAmount => 'Importe';

  @override
  String get invoiceEditPoNumber => 'N.º de pedido';

  @override
  String get invoiceEditGlAccount => 'Cuenta contable';

  @override
  String get invoiceEditDescription => 'Descripción';

  @override
  String get invoiceEditDueDate => 'Fecha de vencimiento';

  @override
  String get invoiceEditNotSet => 'Sin definir';

  @override
  String get invoiceEditInvalidAmount =>
      'Introduce un importe válido (p. ej. 1234,56)';

  @override
  String get invoiceEditClearDueDate => 'Borrar fecha de vencimiento';

  @override
  String get invoiceEditLockedNotice =>
      'Aprobada: el beneficiario y el importe están bloqueados. Para cambiarlos, rechace la factura, corríjala y apruébela de nuevo.';

  @override
  String get invoiceEditLockedHelper => 'Bloqueado tras la aprobación';

  @override
  String invoiceEditDueDateHint(String value) {
    return 'Fecha de vencimiento, actualmente $value. Toca dos veces para cambiar.';
  }

  @override
  String get warningsSectionTitle => 'Avisos y alertas de fraude';

  @override
  String get warningsPoMatchTitle => 'Conciliación de pedido';

  @override
  String get warningsSeverityError => 'Error';

  @override
  String get warningsSeverityWarning => 'Aviso';

  @override
  String get warningsSeverityInfo => 'Información';

  @override
  String get warningsPoLabel => 'Pedido';

  @override
  String warningsMatchLabel(String type) {
    return 'Conciliación $type';
  }

  @override
  String warningsVarianceLabel(String value) {
    return '$value % de variación';
  }

  @override
  String get erpStatusTitle => 'Estado del ERP';

  @override
  String get erpStatusReference => 'Referencia del ERP';

  @override
  String get erpStatusDocumentId => 'ID de documento';

  @override
  String get erpStatusError => 'Error';

  @override
  String get erpStatusLastUpdate => 'Última actualización';

  @override
  String get erpStatusStatus => 'Estado';

  @override
  String get fileViewerPdfTitle => 'PDF de la factura';

  @override
  String get fileViewerImageTitle => 'Imagen de la factura';

  @override
  String get fileViewerPdfError => 'No se pudo cargar el PDF';

  @override
  String get fileViewerImageError => 'No se pudo cargar la imagen';

  @override
  String get fileViewerRetry => 'Reintentar';

  @override
  String get timelineNoActivity => 'Aún no hay actividad';

  @override
  String get payTitle => 'Pagar';

  @override
  String get payTabQueue => 'Cola';

  @override
  String get payTabRuns => 'Lotes';

  @override
  String get paySummaryTotalPaid => 'Total pagado';

  @override
  String get paySummaryPending => 'Pendiente';

  @override
  String get paySummaryInQueue => 'En cola';

  @override
  String get paySummaryCardRebates => 'Reembolsos de tarjeta';

  @override
  String paySummaryPaymentsSubtitle(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count pagos',
      one: '$count pago',
    );
    return '$_temp0';
  }

  @override
  String get payQueueEmpty => 'No hay facturas pendientes de pago';

  @override
  String get payQueueError => 'No se pudo cargar la cola de pagos';

  @override
  String get payQueueRetry => 'Reintentar';

  @override
  String payQueueDue(String date) {
    return 'Vence $date';
  }

  @override
  String get payQueueNoDueDate => 'Sin fecha de vencimiento';

  @override
  String payQueueDiscount(String amount) {
    return 'descuento $amount';
  }

  @override
  String get payQueueOverdue => 'vencida';

  @override
  String get payQueueSelected => 'seleccionada';

  @override
  String get payQueueBlockedDuplicate => 'Posible duplicado: sin resolver';

  @override
  String get payQueueBlockedFraudFlag => 'Alerta de fraude: sin resolver';

  @override
  String get payQueueBlockedLineTotalMismatch =>
      'Los totales de las líneas no cuadran: sin resolver';

  @override
  String get payQueueBlockedPaymentReconciliation =>
      'Pago anterior sin conciliar: puede seguir en tránsito';

  @override
  String get payQueueBlockedFullyCredited =>
      'Cubierta por completo con notas de crédito: nada que pagar';

  @override
  String get payQueueBlockedLiveVirtualCard =>
      'Una tarjeta virtual activa cubre esta factura: páguela con tarjeta';

  @override
  String get payQueueBlockedGeneric =>
      'Una excepción sin resolver bloquea el pago';

  @override
  String payQueueBlockedAnnounce(String reason) {
    return 'no se puede pagar: $reason';
  }

  @override
  String payQueuePinnedMethod(String method) {
    return 'Pagar con $method';
  }

  @override
  String payMethodLabel(String invoiceNumber) {
    return 'Método de pago para $invoiceNumber';
  }

  @override
  String get payMethodAch => 'ACH';

  @override
  String get payMethodWire => 'Transferencia';

  @override
  String get payMethodCheck => 'Cheque';

  @override
  String get payMethodVirtualCard => 'Tarjeta virtual';

  @override
  String paySelectedCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count facturas seleccionadas',
      one: '$count factura seleccionada',
    );
    return '$_temp0';
  }

  @override
  String get payClear => 'Borrar';

  @override
  String get payCreateRun => 'Crear lote';

  @override
  String payCreateRunFailed(String error) {
    return 'No se pudo crear el lote: $error';
  }

  @override
  String get payRunsEmpty => 'No hay lotes de pago';

  @override
  String payRunSubtitle(int count, String date) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count pagos',
      one: '$count pago',
    );
    return '$_temp0 • $date';
  }

  @override
  String get payRunCfoRequiredSuffix => ' • Se requiere aprobación del CFO';

  @override
  String payRunAnnounce(String amount, String status, String subtitle) {
    return 'Lote $amount, $status, $subtitle';
  }

  @override
  String get payRunActions => 'Acciones del lote';

  @override
  String get payRunActionExecute => 'Ejecutar';

  @override
  String get payRunActionCancel => 'Cancelar';

  @override
  String get payRunActionApprove => 'Aprobar como CFO';

  @override
  String get payRunApproveTitle => '¿Aprobar el lote de pago?';

  @override
  String payRunApproveBody(String date, int count, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count pagos',
      one: '$count pago',
    );
    return 'Aprobación del lote creado el $date: $_temp0 por un total de $amount. Esto autoriza la ejecución; no mueve dinero.';
  }

  @override
  String get payRunApproveConfirm => 'Aprobar';

  @override
  String payRunApproveFailed(String error) {
    return 'Error al aprobar: $error';
  }

  @override
  String get payRunCfoBlocked =>
      'Este lote necesita la aprobación del CFO antes de poder ejecutarse.';

  @override
  String get payRunExecuteTitle => '¿Ejecutar el lote de pago?';

  @override
  String payRunExecuteBody(String amount) {
    return 'Esto envía $amount mediante el procesador de pagos configurado.';
  }

  @override
  String payRunExecuteFailed(String error) {
    return 'Error al ejecutar: $error';
  }

  @override
  String payRunCancelFailed(String error) {
    return 'Error al cancelar: $error';
  }

  @override
  String get payRunStatusDraft => 'Borrador';

  @override
  String get payRunStatusCompleted => 'Completado';

  @override
  String get payRunStatusSubmitted => 'Enviado';

  @override
  String get payRunStatusPartial => 'Parcial';

  @override
  String get payRunStatusFailed => 'Fallido';

  @override
  String get payRunStatusCancelled => 'Cancelado';

  @override
  String get payConfirmCancel => 'Cancelar';

  @override
  String get payConfirmExecute => 'Ejecutar';

  @override
  String get loginAppName => 'FeohLedger';

  @override
  String get loginTagline => 'Cuentas por pagar, simplificadas';

  @override
  String get loginTenant => 'Inquilino';

  @override
  String get loginEmail => 'Correo electrónico';

  @override
  String get loginPassword => 'Contraseña';

  @override
  String get loginShowPassword => 'Mostrar contraseña';

  @override
  String get loginHidePassword => 'Ocultar contraseña';

  @override
  String get loginRequired => 'Obligatorio';

  @override
  String get loginSignIn => 'Iniciar sesión';

  @override
  String get mfaTitle => 'Autenticación de dos factores';

  @override
  String get mfaHeading => 'Verifica tu identidad';

  @override
  String get mfaPromptEmail =>
      'Introduce el código de 6 dígitos que te enviamos por correo electrónico.';

  @override
  String get mfaPromptTotp =>
      'Introduce el código de 6 dígitos de tu aplicación de autenticación.';

  @override
  String get mfaEnforcedNotice =>
      'Tu organización requiere autenticación de dos factores. Verifica ahora con un código por correo electrónico y luego termina de configurar una aplicación de autenticación en la aplicación web.';

  @override
  String get mfaCode => 'Código';

  @override
  String get mfaCodeRequired => 'Obligatorio';

  @override
  String get mfaCodeTooShort => 'Introduce al menos 6 dígitos';

  @override
  String get mfaVerify => 'Verificar';

  @override
  String get mfaSending => 'Enviando…';

  @override
  String get mfaResendEmailCode => 'Reenviar código por correo';

  @override
  String get mfaSendEmailCode => 'Enviar código por correo';

  @override
  String get mfaUseEmailInstead => 'Usar un código por correo en su lugar';

  @override
  String get mfaUseAuthenticatorInstead =>
      'Usar la aplicación de autenticación en su lugar';

  @override
  String get mfaEmailedAnnounce =>
      'Se envió un código de inicio de sesión a tu correo electrónico.';

  @override
  String get adminUsersTitle => 'Gestión de usuarios';

  @override
  String get adminUsersSearchHint => 'Buscar por nombre o correo electrónico';

  @override
  String get adminUsersEmpty => 'No se encontraron usuarios';

  @override
  String get adminUsersLoadError => 'No se pudieron cargar los usuarios';

  @override
  String get adminUsersEditRoles => 'Editar roles';

  @override
  String get adminUsersNoRoles => 'Sin roles';

  @override
  String get adminUsersDeactivate => 'Desactivar usuario';

  @override
  String get adminUsersActivate => 'Activar usuario';

  @override
  String get adminUsersCannotDeactivateSelf =>
      'No puedes desactivar tu propia cuenta';

  @override
  String get adminUsersDeactivateHint =>
      'Los cierra la sesión y bloquea el inicio de sesión';

  @override
  String get adminUsersActivateHint => 'Restaura el acceso de inicio de sesión';

  @override
  String get adminUsersRoleActive => 'activo';

  @override
  String get adminUsersRoleInactive => 'inactivo';

  @override
  String get adminUsersInactiveBadge => 'Inactivo';

  @override
  String adminUsersRolesUpdated(String name) {
    return 'Roles actualizados para $name';
  }

  @override
  String adminUsersRolesUpdateFailed(String error) {
    return 'No se pudieron actualizar los roles: $error';
  }

  @override
  String adminUsersActivated(String name) {
    return '$name activado';
  }

  @override
  String adminUsersDeactivated(String name) {
    return '$name desactivado';
  }

  @override
  String adminUsersUpdateFailed(String name, String error) {
    return 'No se pudo actualizar $name: $error';
  }

  @override
  String get adminUsersCreateUser => 'Crear usuario';

  @override
  String get adminUsersCreateTitle => 'Nuevo usuario';

  @override
  String get adminUsersFieldFullName => 'Nombre completo';

  @override
  String get adminUsersFieldEmail => 'Correo electrónico';

  @override
  String get adminUsersFieldRoles => 'Roles';

  @override
  String get adminUsersValidationNameRequired =>
      'El nombre completo es obligatorio';

  @override
  String get adminUsersValidationEmailRequired =>
      'El correo electrónico es obligatorio';

  @override
  String get adminUsersValidationEmailInvalid =>
      'Introduce una dirección de correo válida';

  @override
  String get adminUsersCreateSubmit => 'Crear';

  @override
  String get adminUsersCreating => 'Creando…';

  @override
  String adminUsersCreated(String name) {
    return '$name creado';
  }

  @override
  String adminUsersCreateFailed(String error) {
    return 'No se pudo crear el usuario: $error';
  }

  @override
  String get adminUsersTempPasswordTitle => 'Usuario creado';

  @override
  String adminUsersTempPasswordBody(String name) {
    return 'Comparte esta contraseña de un solo uso con $name. Se le pedirá que la cambie al iniciar sesión por primera vez. No se mostrará de nuevo.';
  }

  @override
  String get adminUsersDelete => 'Eliminar usuario';

  @override
  String get adminUsersDeleteHint => 'Elimina esta cuenta de forma permanente';

  @override
  String get adminUsersCannotDeleteSelf =>
      'No puedes eliminar tu propia cuenta';

  @override
  String adminUsersDeleteConfirmTitle(String name) {
    return '¿Eliminar a $name?';
  }

  @override
  String adminUsersDeleteConfirmBody(String name, String email) {
    return 'Esto elimina permanentemente a $name ($email). No se puede deshacer.';
  }

  @override
  String adminUsersDeleted(String name) {
    return '$name eliminado';
  }

  @override
  String adminUsersDeleteFailed(String name, String error) {
    return 'No se pudo eliminar a $name: $error';
  }

  @override
  String get orgSettingsTitle => 'Configuración de la organización';

  @override
  String get orgSettingsNoSettings => 'Sin configuración';

  @override
  String get orgSettingsLoadError => 'No se pudo cargar la configuración';

  @override
  String get orgSettingsSectionCompany => 'Empresa';

  @override
  String get orgSettingsSectionInvoiceDefaults =>
      'Valores predeterminados de factura';

  @override
  String get orgSettingsName => 'Nombre de la organización';

  @override
  String get orgSettingsAddress => 'Dirección';

  @override
  String get orgSettingsPhone => 'Teléfono';

  @override
  String get orgSettingsWebsite => 'Sitio web';

  @override
  String get orgSettingsTaxId => 'Identificación fiscal';

  @override
  String get orgSettingsCurrency => 'Moneda predeterminada';

  @override
  String get orgSettingsPaymentTerms => 'Condiciones de pago';

  @override
  String get orgSettingsNumberPrefix => 'Prefijo de número de factura';

  @override
  String get orgSettingsGlAccount => 'Cuenta contable predeterminada';

  @override
  String get orgSettingsCostCenter => 'Centro de costos predeterminado';

  @override
  String get orgSettingsSave => 'Guardar cambios';

  @override
  String get orgSettingsSaving => 'Guardando…';

  @override
  String orgSettingsFieldRequired(String label) {
    return '$label es obligatorio';
  }

  @override
  String get orgSettingsSaved => 'Configuración de la organización guardada';

  @override
  String orgSettingsSaveFailed(String error) {
    return 'Error al guardar: $error';
  }

  @override
  String get workflowsTitle => 'Flujos de trabajo';

  @override
  String get workflowsEmpty => 'No se encontraron flujos de trabajo';

  @override
  String get workflowsLoadError =>
      'No se pudieron cargar los flujos de trabajo';

  @override
  String get workflowsStatusActive => 'Activo';

  @override
  String get workflowsStatusInactive => 'Inactivo';

  @override
  String get workflowsDefault => 'Predeterminado';

  @override
  String workflowsStepCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count pasos',
      one: '$count paso',
    );
    return '$_temp0';
  }

  @override
  String get workflowDetailFallbackTitle => 'Flujo de trabajo';

  @override
  String get workflowDetailLoadError => 'No se pudo cargar el flujo de trabajo';

  @override
  String get workflowDetailNoSteps => 'Este flujo de trabajo no tiene pasos.';

  @override
  String get workflowDetailDefaultWorkflow => 'Flujo de trabajo predeterminado';

  @override
  String workflowDetailStepNumber(int number) {
    return 'Paso $number';
  }

  @override
  String get workflowDetailStepEnabled => 'Habilitado';

  @override
  String get workflowDetailStepDisabled => 'Deshabilitado';

  @override
  String workflowDetailApproverCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count aprobadores',
      one: '$count aprobador',
    );
    return '$_temp0';
  }

  @override
  String workflowDetailDelaySummary(String hours) {
    return 'Retraso $hours h';
  }

  @override
  String workflowDetailConditionSummary(String field) {
    return 'En $field';
  }

  @override
  String get cashFlowTitle => 'Previsión de flujo de caja';

  @override
  String cashFlowErrorPrefix(String error) {
    return 'Error: $error';
  }

  @override
  String cashFlowHorizonDays(int days) {
    return '$days días';
  }

  @override
  String get cashFlowLowBalanceAlert => 'Alerta de saldo bajo';

  @override
  String cashFlowBreachSingle(
    String threshold,
    String period,
    String shortfall,
  ) {
    return 'Se prevé caer por debajo del saldo $threshold en $period (déficit $shortfall).';
  }

  @override
  String cashFlowBreachMultiple(int count, String period, String shortfall) {
    return 'Se prevé que $count periodos caigan por debajo del saldo mínimo. Peor: $period, déficit $shortfall.';
  }

  @override
  String get cashFlowMinimum => 'mínimo';

  @override
  String get cashFlowOpeningBalance => 'Saldo inicial';

  @override
  String get cashFlowProjectedEnd => 'Final previsto';

  @override
  String cashFlowProjectedEndSubtitle(int days) {
    return 'en $days días';
  }

  @override
  String get cashFlowCommittedOut => 'Salida comprometida';

  @override
  String get cashFlowCommittedSubtitle => 'compromisos firmes';

  @override
  String get cashFlowPendingOut => 'Salida pendiente';

  @override
  String get cashFlowPendingSubtitle => 'flujo en curso';

  @override
  String get cashFlowOpeningSourceProvider => 'sincronizado del banco';

  @override
  String get cashFlowOpeningSourceSettings => 'saldo guardado';

  @override
  String get cashFlowOpeningSourceQuery => 'manual';

  @override
  String get cashFlowOpeningSourceUnset => 'establecer un saldo';

  @override
  String get cashFlowProjectedOutflows => 'Salidas previstas';

  @override
  String get cashFlowNoOutflows =>
      'No hay salidas previstas en este horizonte.';

  @override
  String cashFlowInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count facturas',
      one: '$count factura',
    );
    return '$_temp0';
  }

  @override
  String cashFlowCommittedAmount(String amount) {
    return 'comprometido $amount';
  }

  @override
  String cashFlowPendingAmount(String amount) {
    return 'pendiente $amount';
  }

  @override
  String get cashFlowPosition => 'Posición de caja';

  @override
  String get cashFlowNoPosition =>
      'No hay previsión de posición de caja para este horizonte.';

  @override
  String cashFlowOutAmount(String amount) {
    return 'salida $amount';
  }

  @override
  String cashFlowForecastRowLabel(
    String period,
    String scheduled,
    String committed,
    String pending,
    int count,
  ) {
    return '$period: programado $scheduled, comprometido $committed, pendiente $pending, $count facturas';
  }

  @override
  String cashFlowPositionRowLabel(
    String period,
    String opening,
    String outflow,
    String closing,
  ) {
    return '$period: inicial $opening, salida $outflow, final $closing';
  }

  @override
  String get cashFlowBelowThresholdSuffix => ', por debajo del umbral';

  @override
  String cashFlowLowBalanceAlertLabel(String message) {
    return 'Alerta de saldo bajo. $message';
  }

  @override
  String get contractsTitle => 'Contratos';

  @override
  String get contractsSearchHint => 'Buscar contratos…';

  @override
  String get contractsEmpty => 'No se encontraron contratos';

  @override
  String get contractsFilterDraft => 'Borrador';

  @override
  String get contractsFilterActive => 'Activo';

  @override
  String get contractsFilterExpired => 'Vencido';

  @override
  String get contractsFilterTerminated => 'Rescindido';

  @override
  String get contractsFilterCancelled => 'Cancelado';

  @override
  String get contractDetailTitle => 'Detalle del contrato';

  @override
  String contractDetailErrorPrefix(String error) {
    return 'Error: $error';
  }

  @override
  String get contractDetailUntitled => 'Contrato sin título';

  @override
  String get contractDetailFieldContractNumber => 'N.º de contrato';

  @override
  String get contractDetailFieldVendor => 'Proveedor';

  @override
  String get contractDetailFieldType => 'Tipo';

  @override
  String get contractDetailFieldCurrency => 'Moneda';

  @override
  String get contractDetailFieldSpendLimit => 'Límite de gasto';

  @override
  String get contractDetailNotToExceed => ' (no superar)';

  @override
  String get contractDetailFieldStartDate => 'Fecha de inicio';

  @override
  String get contractDetailFieldEndDate => 'Fecha de fin';

  @override
  String get contractDetailFieldSigned => 'Firmado';

  @override
  String get contractDetailFieldAutoRenew => 'Renovación automática';

  @override
  String get contractDetailYes => 'Sí';

  @override
  String get contractDetailNo => 'No';

  @override
  String get contractDetailFieldRenewalTerm => 'Plazo de renovación';

  @override
  String contractDetailRenewalTermMonths(int months) {
    return '$months meses';
  }

  @override
  String get contractDetailFieldRenewalNotice => 'Aviso de renovación';

  @override
  String contractDetailRenewalNoticeDays(int days) {
    return '$days días';
  }

  @override
  String get contractDetailFieldPaymentTerms => 'Condiciones de pago';

  @override
  String get contractDetailFieldDescription => 'Descripción';

  @override
  String get contractDetailFieldCreated => 'Creado';

  @override
  String get contractDetailSectionSpend => 'Gasto';

  @override
  String get contractDetailSectionLineItems => 'Líneas';

  @override
  String get contractDetailSpendInvoiced => 'Facturado';

  @override
  String contractDetailSpendInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count facturas',
      one: '$count factura',
    );
    return '$_temp0';
  }

  @override
  String get contractDetailSpendOverLimit => 'Sobre el límite';

  @override
  String get contractDetailSpendRemaining => 'Restante';

  @override
  String contractDetailSpendOfLimit(String limit) {
    return 'de $limit';
  }

  @override
  String get contractDetailSpendNoLimit => 'sin límite establecido';

  @override
  String get contractDetailLineItemFallback => 'Línea';

  @override
  String contractDetailLineQty(String quantity) {
    return 'Cant. $quantity';
  }

  @override
  String contractDetailLineUnitPrice(String price) {
    return '@ $price';
  }

  @override
  String contractDetailLineGl(String account) {
    return 'Cuenta $account';
  }

  @override
  String get contractActivate => 'Activar';

  @override
  String get contractActivated => 'Contrato activado';

  @override
  String get contractActivateFailed =>
      'No se pudo activar el contrato; inténtalo de nuevo';

  @override
  String get contractTerminate => 'Rescindir';

  @override
  String get contractTerminateTitle => 'Rescindir contrato';

  @override
  String get contractTerminateBody =>
      'Esto finaliza el contrato anticipadamente. No se puede deshacer. ¿Continuar?';

  @override
  String get contractTerminated => 'Contrato rescindido';

  @override
  String get contractTerminateFailed =>
      'No se pudo rescindir el contrato; inténtalo de nuevo';

  @override
  String get exceptionDetailTitle => 'Excepción';

  @override
  String get exceptionDetailNotFound => 'Excepción no encontrada';

  @override
  String get exceptionDetailOverdue => 'Vencida';

  @override
  String get exceptionDetailSectionDescription => 'Descripción';

  @override
  String get exceptionDetailSectionInvoice => 'Factura';

  @override
  String get exceptionDetailNoLinkedInvoice => 'Sin factura vinculada';

  @override
  String get exceptionDetailFieldNumber => 'Número';

  @override
  String get exceptionDetailFieldVendor => 'Proveedor';

  @override
  String get exceptionDetailFieldAmount => 'Importe';

  @override
  String get exceptionDetailFieldSeverity => 'Gravedad';

  @override
  String get exceptionDetailSectionSla => 'SLA';

  @override
  String get exceptionDetailFieldCreated => 'Creada';

  @override
  String get exceptionDetailFieldDue => 'Vence';

  @override
  String get exceptionDetailNoSla => 'Sin SLA establecido';

  @override
  String get exceptionDetailFieldStatus => 'Estado';

  @override
  String get exceptionDetailOnTrack => 'En plazo';

  @override
  String get exceptionDetailResolvedIn => 'Resuelta en';

  @override
  String exceptionDetailResolvedInHours(String hours) {
    return '$hours h';
  }

  @override
  String get exceptionDetailSectionAssignee => 'Asignada a';

  @override
  String get exceptionDetailUnassigned => 'Sin asignar';

  @override
  String get exceptionDetailAssign => 'Asignar';

  @override
  String get exceptionDetailReassign => 'Reasignar';

  @override
  String get exceptionDetailSectionResolution => 'Resolución';

  @override
  String get exceptionDetailResolutionNote => 'Nota';

  @override
  String get exceptionDetailResolutionBy => 'Por';

  @override
  String get exceptionDetailResolutionAt => 'El';

  @override
  String get exceptionDetailActionResolved => 'Excepción resuelta';

  @override
  String get exceptionDetailActionEscalated => 'Excepción escalada';

  @override
  String get exceptionDetailActionDismissed => 'Excepción descartada';

  @override
  String get exceptionDetailActionResolveFailed =>
      'No se pudo resolver la excepción';

  @override
  String get exceptionDetailActionEscalateFailed =>
      'No se pudo escalar la excepción';

  @override
  String get exceptionDetailActionDismissFailed =>
      'No se pudo descartar la excepción';

  @override
  String get exceptionDetailAssignTo => 'Asignar a';

  @override
  String get exceptionDetailUnassign => 'Quitar asignación';

  @override
  String exceptionDetailLoadUsersFailed(String error) {
    return 'No se pudieron cargar los usuarios: $error';
  }

  @override
  String get exceptionDetailAssigneeUpdateFailed =>
      'No se pudo actualizar la persona asignada';

  @override
  String get exceptionDetailUnassigned2 =>
      'Asignación de la excepción retirada';

  @override
  String exceptionDetailAssignedTo(String name) {
    return 'Asignada a $name';
  }

  @override
  String get settingsProcurement => 'Compras';

  @override
  String get settingsInspections => 'Inspecciones de calidad';

  @override
  String get settingsInspectionsHint =>
      'Registrar y consultar inspecciones del cotejo de 4 vías';

  @override
  String get settingsAdministration => 'Administración';

  @override
  String get settingsAdminUsers => 'Gestión de usuarios';

  @override
  String get settingsAdminUsersHint => 'Roles, activar / desactivar usuarios';

  @override
  String get settingsAdminOrg => 'Ajustes de la organización';

  @override
  String get settingsAdminOrgHint =>
      'Perfil de la empresa, valores por defecto de facturas';

  @override
  String get settingsAdminWorkflows => 'Flujos de trabajo';

  @override
  String get settingsAdminWorkflowsHint =>
      'Ver definiciones de flujo y sus pasos';

  @override
  String get settingsAdaptive => 'Flujos adaptativos';

  @override
  String get settingsAdaptiveHint =>
      'Patrones de aprobación, anomalías, sugerencias';

  @override
  String get inspectionsTitle => 'Inspecciones de calidad';

  @override
  String get inspectionsRecord => 'Registrar inspección';

  @override
  String get inspectionsEmpty => 'No hay inspecciones de calidad registradas.';

  @override
  String get inspectionsEmptyFiltered =>
      'No hay inspecciones con este resultado.';

  @override
  String get inspectionsLoadError => 'No se pudieron cargar las inspecciones';

  @override
  String get inspectionsResultPass => 'Aprobada';

  @override
  String get inspectionsResultFail => 'Rechazada';

  @override
  String get inspectionsResultPartial => 'Aceptación parcial';

  @override
  String get inspectionsResultUnknown => 'Resultado desconocido';

  @override
  String inspectionsResultAnnounce(String result) {
    return 'Resultado: $result';
  }

  @override
  String get inspectionsNotLinked => 'Sin vincular';

  @override
  String get inspectionsNotLinkedHint =>
      'Esta inspección no está ligada a ninguna recepción ni orden de compra, por lo que el cotejo nunca la leerá.';

  @override
  String get inspectionsReceiptUnnamed => 'Recepción sin nombre';

  @override
  String get inspectionsHintPass => 'Mercancía aceptada: el cotejo no cambia.';

  @override
  String get inspectionsHintFail =>
      'Mercancía rechazada: la factura pasa a discrepancia y una retención de calidad bloquea el pago.';

  @override
  String get inspectionsHintPartial =>
      'Parte de la mercancía aceptada: el cotejo pasa a parcial y se marca la cantidad aceptada.';

  @override
  String get inspectionsHintUnknown =>
      'Este resultado está fuera del vocabulario aprobada / rechazada / parcial, así que no se puede indicar su efecto en el cotejo.';

  @override
  String inspectionsRecorded(String number) {
    return 'Inspección $number registrada';
  }

  @override
  String inspectionsRecordFailed(String error) {
    return 'No se pudo registrar la inspección: $error';
  }

  @override
  String inspectionsReceiptsLoadFailed(String error) {
    return 'No se pudieron cargar las recepciones: $error';
  }

  @override
  String get inspectionRecordTitle => 'Registrar inspección de calidad';

  @override
  String get inspectionRecordClose => 'Cerrar el formulario de inspección';

  @override
  String get inspectionRecordReceipt => 'Recepción';

  @override
  String get inspectionRecordReceiptHint =>
      'El cotejo solo lee una inspección a través de su recepción, así que la inspección debe indicar la entrega que cubre.';

  @override
  String inspectionRecordReceiptsBounded(int shown, int total) {
    return 'Se muestran las $shown recepciones más recientes de $total. Regístrela desde la aplicación web si la entrega que busca no aparece.';
  }

  @override
  String get inspectionRecordNoReceipts =>
      'Aún no hay recepciones: una inspección cubre una entrega, así que no hay nada que registrar.';

  @override
  String get inspectionRecordNumber => 'N.º de inspección';

  @override
  String get inspectionRecordNumberRequired =>
      'Introduzca un n.º de inspección';

  @override
  String get inspectionRecordResult => 'Resultado';

  @override
  String get inspectionRecordAcceptedQuantity => 'Cantidad aceptada';

  @override
  String get inspectionRecordRejectedQuantity => 'Cantidad rechazada';

  @override
  String get inspectionRecordAcceptedRequired =>
      'Obligatoria en una aceptación parcial';

  @override
  String get inspectionRecordInvalidQuantity => 'Hasta 8 dígitos y 4 decimales';

  @override
  String get inspectionRecordInspectedDate => 'Fecha de inspección';

  @override
  String get inspectionRecordDateNotSet => 'Sin definir';

  @override
  String get inspectionRecordClearDate => 'Borrar la fecha de inspección';

  @override
  String get inspectionRecordInspector => 'Inspector';

  @override
  String get inspectionRecordNotes => 'Notas de desviación';

  @override
  String get inspectionRecordNotesHint =>
      'Se citan literalmente en la incidencia de cotejo de la factura cuando la inspección se rechaza, para quien atienda la retención de calidad.';

  @override
  String get inspectionRecordSubmit => 'Registrar inspección';

  @override
  String get inspectionDetailTitle => 'Inspección';

  @override
  String get inspectionDetailNotFound => 'Inspección no encontrada';

  @override
  String inspectionDetailErrorPrefix(String error) {
    return 'No se pudo cargar la inspección: $error';
  }

  @override
  String get inspectionDetailFieldReceipt => 'Recepción';

  @override
  String get inspectionDetailFieldInspectedDate => 'Inspeccionada';

  @override
  String get inspectionDetailFieldInspector => 'Inspector';

  @override
  String get inspectionDetailFieldAccepted => 'Cantidad aceptada';

  @override
  String get inspectionDetailFieldRejected => 'Cantidad rechazada';

  @override
  String get inspectionDetailFieldStatus => 'Estado';

  @override
  String get inspectionDetailFieldCreated => 'Creada';

  @override
  String get inspectionDetailSectionNotes => 'Notas de desviación';

  @override
  String get adaptiveTitle => 'Flujos adaptativos';

  @override
  String get adaptiveTabSuggestions => 'Sugerencias';

  @override
  String get adaptiveTabPatterns => 'Patrones de aprobación';

  @override
  String get adaptiveTabAnomalies => 'Anomalías';

  @override
  String get adaptiveAdvisoryNote =>
      'Todo aquí es orientativo. Nada en esta pantalla ha cambiado ningún flujo: una recomendación solo surte efecto cuando alguien con el rol adecuado la aplica en la aplicación web, por la misma vía auditada que una edición manual.';

  @override
  String get adaptiveSuggestionsEmpty =>
      'Sin sugerencias: todavía no hay historial de aprobación suficientemente consistente.';

  @override
  String get adaptiveSuggestionsError =>
      'No se pudieron cargar las sugerencias.';

  @override
  String get adaptiveSuggestionsShowOpen => 'Abiertas';

  @override
  String get adaptiveSuggestionsShowAll => 'Todas';

  @override
  String adaptiveSuggestionsConfidence(String pct) {
    return 'Confianza $pct %';
  }

  @override
  String get adaptiveSuggestionsDismiss => 'Descartar';

  @override
  String get adaptiveSuggestionsDismissTitle => '¿Descartar esta sugerencia?';

  @override
  String get adaptiveSuggestionsDismissBody =>
      'Seguirá descartada tras el recálculo, y en ningún caso cambia ningún flujo.';

  @override
  String get adaptiveSuggestionsDismissed => 'Sugerencia descartada.';

  @override
  String adaptiveSuggestionsDismissFailed(String error) {
    return 'No se pudo descartar esa sugerencia: $error';
  }

  @override
  String get adaptiveSuggestionStatusOpen => 'Abierta';

  @override
  String get adaptiveSuggestionStatusDismissed => 'Descartada';

  @override
  String get adaptiveSuggestionStatusApplied => 'Aplicada';

  @override
  String get adaptiveSuggestionStatusStale => 'Obsoleta';

  @override
  String get adaptiveSuggestionStatusUnknown => 'Estado desconocido';

  @override
  String adaptiveSuggestionStatusAnnounce(String status) {
    return 'Estado: $status';
  }

  @override
  String get adaptivePatternsError =>
      'No se pudieron cargar los patrones de aprobación.';

  @override
  String get adaptivePatternsEmptyApprovers =>
      'Aún no hay decisiones de aprobación en esta ventana.';

  @override
  String get adaptivePatternsEmptyVendors =>
      'Aún no hay historial de proveedores en esta ventana.';

  @override
  String get adaptivePatternsSectionApprovers => 'Por aprobador';

  @override
  String get adaptivePatternsSectionVendors => 'Por proveedor';

  @override
  String adaptivePatternsLookback(int days) {
    return 'Estadísticas deterministas sobre los últimos $days días del historial de aprobación de este inquilino: sin modelo, recalculadas cada vez que las consulta.';
  }

  @override
  String get adaptivePatternsCurrencyNote =>
      'Los importes están en la moneda de reporte de su organización.';

  @override
  String get adaptivePatternsUnknownApprover => 'Aprobador desconocido';

  @override
  String adaptivePatternsApproverSummary(int approved, int rejected) {
    return '$approved aprobadas · $rejected rechazadas';
  }

  @override
  String adaptivePatternsApproverTiming(String rate, String days) {
    return 'Tasa de aprobación $rate % · mediana $days días';
  }

  @override
  String adaptivePatternsVendorConsistency(String pct) {
    return '$pct % aprobadas sin editar';
  }

  @override
  String adaptivePatternsVendorMoney(String median, String avg) {
    return 'Mediana $median · promedio $avg';
  }

  @override
  String adaptivePatternsUnconverted(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other:
          '$count aprobaciones no pudieron expresarse en la moneda de reporte y quedan excluidas de los importes de arriba; la muestra sigue contándolas',
      one:
          '$count aprobación no pudo expresarse en la moneda de reporte y queda excluida de los importes de arriba; la muestra sigue contándola',
    );
    return '$_temp0';
  }

  @override
  String get adaptiveAnomaliesIntro =>
      'Facturas en revisión que se salen del patrón establecido de su proveedor: por importe, por aprobador o por cuánto llevan esperando. Solo lectura: marcar aquí no genera nada ni bloquea nada.';

  @override
  String get adaptiveAnomaliesEmpty =>
      'Nada en revisión se sale del patrón normal de su proveedor.';

  @override
  String get adaptiveAnomaliesError =>
      'No se pudo cargar el análisis de anomalías.';

  @override
  String adaptiveAnomaliesScanned(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count facturas en revisión analizadas.',
      one: '$count factura en revisión analizada.',
    );
    return '$_temp0';
  }

  @override
  String adaptiveAnomaliesAmount(String amount, String currency) {
    return '$amount $currency';
  }

  @override
  String get adaptiveAnomaliesInsufficient =>
      'Todavía no hay suficiente historial para este proveedor.';

  @override
  String get invoiceWarningMissingVendorName => 'Falta el nombre del proveedor';

  @override
  String get invoiceWarningMissingInvoiceNumber => 'Falta el número de factura';

  @override
  String get invoiceWarningMissingAmount => 'Importe ausente o cero';

  @override
  String get invoiceWarningDuplicateInvoiceNumber =>
      'Número de factura duplicado para este proveedor';

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
      other: ' (más $crossEntityCount facturas casi idénticas en otra entidad)',
      one: ' (más $crossEntityCount factura casi idéntica en otra entidad)',
      zero: '',
    );
    return 'Posible duplicado: $similarity de coincidencia con $invoiceNumber de $vendorName$_temp0';
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
      other: ' (más $crossEntityCount facturas casi idénticas en otra entidad)',
      one: ' (más $crossEntityCount factura casi idéntica en otra entidad)',
      zero: '',
    );
    return 'Posible duplicado: $similarity de coincidencia con $invoiceNumber$_temp0';
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
      other: ' (más $crossEntityCount facturas casi idénticas en otra entidad)',
      one: ' (más $crossEntityCount factura casi idéntica en otra entidad)',
      zero: '',
    );
    return 'Posible duplicado: $similarity de coincidencia con otra factura de $vendorName$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarUnnumberedUnnamedVendor(
    String similarity,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other: ' (más $crossEntityCount facturas casi idénticas en otra entidad)',
      one: ' (más $crossEntityCount factura casi idéntica en otra entidad)',
      zero: '',
    );
    return 'Posible duplicado: $similarity de coincidencia con otra factura$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarCrossEntity(String similarity) {
    return 'Posible duplicado: $similarity de coincidencia con una factura casi idéntica en otra entidad';
  }

  @override
  String invoiceWarningRoundAmount(String amount) {
    return 'Importe redondo: $amount';
  }

  @override
  String get invoiceWarningFutureInvoiceDate =>
      'La fecha de la factura está en el futuro';

  @override
  String invoiceWarningRushPayment(int days) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days días',
      one: '$days día',
    );
    return 'Pago urgente: vence en $_temp0 desde la fecha de la factura';
  }

  @override
  String get invoiceWarningPastDue => 'La factura está vencida';

  @override
  String get invoiceWarningUnverifiedVendor =>
      'El proveedor no está verificado';

  @override
  String invoiceWarningPersonalEmailDomain(String domain) {
    return 'El correo del proveedor usa un dominio personal: $domain';
  }

  @override
  String invoiceWarningNewVendorLargeAmount(int days, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days días',
      one: '$days día',
    );
    return 'Proveedor nuevo (creado hace $_temp0) que presenta una factura elevada de $amount';
  }

  @override
  String get invoiceWarningRemitToChanged =>
      'La dirección de pago ha cambiado desde la última factura aprobada de este proveedor';

  @override
  String invoiceWarningAmountAboveVendorMean(
    String amount,
    String sigma,
    String mean,
  ) {
    return 'El importe $amount está $sigmaσ por encima de la media histórica de este proveedor ($mean)';
  }

  @override
  String invoiceWarningLlmAnomaly(String reason) {
    return 'Anomalía detectada por la IA: $reason';
  }

  @override
  String invoiceWarningLineTotalMismatch(
    String lineItemsTotal,
    String headerAmount,
  ) {
    return 'El total de las líneas es $lineItemsTotal, pero el importe de la factura es $headerAmount';
  }

  @override
  String invoiceWarningPriceVarianceOver(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Precio unitario $deltaPct por encima de la referencia de este proveedor para $item ($unitPrice frente a $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPriceVarianceUnder(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Precio unitario $deltaPct por debajo de la referencia de este proveedor para $item ($unitPrice frente a $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPoNotFound(String poNumber) {
    return 'No se encontró el pedido $poNumber';
  }

  @override
  String invoiceWarningPoAmountVariance(
    String variancePct,
    String poNumber,
    String invoiceAmount,
    String poTotal,
  ) {
    return 'Variación de importe $variancePct frente al pedido $poNumber (factura $invoiceAmount frente a pedido $poTotal)';
  }

  @override
  String invoiceWarningPoPartialReceipt(String matchType, String poNumber) {
    return 'Coincidencia parcial de 3 vías — coincidencia $matchType con el pedido $poNumber, pero solo se ha recibido parte de la cantidad pedida';
  }

  @override
  String invoiceWarningPoOverReceipt(
    String receivedQuantity,
    String orderedQuantity,
    String excessQuantity,
    String poNumber,
  ) {
    return 'Exceso de recepción: $receivedQuantity recibidas frente a $orderedQuantity pedidas (+$excessQuantity) en el pedido $poNumber';
  }

  @override
  String invoiceWarningPoOverReceiptUnquantified(String poNumber) {
    return 'Se han recibido más mercancías de las pedidas en el pedido $poNumber';
  }

  @override
  String invoiceWarningQualityInspectionFailed(String poNumber) {
    return 'Inspección de calidad no superada para el pedido $poNumber';
  }

  @override
  String invoiceWarningQualityInspectionFailedNotes(
    String poNumber,
    String notes,
  ) {
    return 'Inspección de calidad no superada para el pedido $poNumber: $notes';
  }

  @override
  String invoiceWarningQualityInspectionMissing(String poNumber) {
    return 'Se requiere inspección de calidad para el pedido $poNumber, pero no existe';
  }

  @override
  String invoiceWarningQualityPartialAcceptance(
    String acceptedQuantity,
    String poNumber,
  ) {
    return 'Aceptación parcial: $acceptedQuantity de la cantidad pedida aceptada en el pedido $poNumber';
  }

  @override
  String invoiceWarningQualityPartialAcceptanceUnquantified(String poNumber) {
    return 'Aceptación parcial de calidad en la inspección del pedido $poNumber';
  }

  @override
  String invoiceWarningRecurringVarianceOver(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'El importe $amount está $deltaPct por encima del importe previsto $expectedAmount de la plantilla recurrente «$templateName»';
  }

  @override
  String invoiceWarningRecurringVarianceUnder(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'El importe $amount está $deltaPct por debajo del importe previsto $expectedAmount de la plantilla recurrente «$templateName»';
  }

  @override
  String invoiceWarningContractExpired(
    String invoiceDate,
    String contractNumber,
    String endDate,
  ) {
    return 'La factura con fecha $invoiceDate es posterior al vencimiento del contrato $contractNumber ($endDate)';
  }

  @override
  String invoiceWarningContractNotStarted(
    String invoiceDate,
    String contractNumber,
    String startDate,
  ) {
    return 'La factura con fecha $invoiceDate es anterior al inicio del contrato $contractNumber ($startDate)';
  }

  @override
  String invoiceWarningContractTerminated(String contractNumber) {
    return 'Gasto registrado contra el contrato rescindido $contractNumber';
  }

  @override
  String invoiceWarningContractCancelled(String contractNumber) {
    return 'Gasto registrado contra el contrato cancelado $contractNumber';
  }

  @override
  String invoiceWarningContractVendorMismatch(String contractNumber) {
    return 'El proveedor de la factura no coincide con el del contrato $contractNumber';
  }

  @override
  String invoiceWarningContractSpendLimitExceeded(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'El gasto acumulado $cumulativeSpend supera el límite $spendLimit del contrato $contractNumber';
  }

  @override
  String invoiceWarningContractSpendLimitExceededNotToExceed(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'El gasto acumulado $cumulativeSpend supera el límite $spendLimit del contrato $contractNumber (no superable)';
  }

  @override
  String invoiceWarningContractGlNotAllowed(
    String glAccount,
    String contractNumber,
  ) {
    return 'La cuenta contable $glAccount está fuera de las cuentas permitidas del contrato $contractNumber';
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
    return 'Los importes no cuadran: subtotal ($subtotal) + impuestos ($tax) + envío ($shipping) − descuento ($discount) = $expected, pero el total es $amount.';
  }

  @override
  String invoiceWarningSelfCorrectionDateOrdering(
    String dueDate,
    String invoiceDate,
  ) {
    return 'La fecha de vencimiento ($dueDate) es anterior a la fecha de la factura ($invoiceDate).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemsSum(
    String lineItemsTotal,
    String amount,
  ) {
    return 'El total de las líneas ($lineItemsTotal) no coincide con el importe de la factura ($amount).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemMath(
    String lineNumber,
    String quantity,
    String unitPrice,
    String expected,
    String total,
  ) {
    return 'Línea $lineNumber: $quantity × $unitPrice = $expected, pero el total es $total.';
  }

  @override
  String invoiceWarningGlCodesNotInChart(String codes) {
    return 'Cuentas contables sugeridas por la IA que no están en el plan activo: $codes';
  }

  @override
  String invoiceWarningGlCodeStalePrior(String code) {
    return 'La cuenta contable «$code» guardada para el proveedor ya no está en el plan de cuentas activo.';
  }
}
