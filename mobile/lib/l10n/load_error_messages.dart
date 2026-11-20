/// The sentence a screen shows when a read it needs fails — the message a
/// detail screen hands [LoadErrorState], or a snackbar's text.
///
/// Never an exception's `toString()`: `ApiException(403): …` and
/// `SocketException: …` are log lines, not sentences, and name neither what
/// happened nor what to do next.
///
/// - An [ApiException] carries the server's own `detail` sentence
///   ([describeApiError]), which says *why* ("Invoice not found", "Not
///   permitted"); [withServerReason] frames it in the screen's own localized
///   words ("Could not load the inspection: …").
/// - Anything else (no connection, a timeout, a malformed body) reached no
///   server verdict, so the reader gets the localized connection sentence.
library;

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';

String describeLoadError(
  AppLocalizations l,
  Object error,
  String Function(String reason) withServerReason,
) =>
    error is ApiException
        ? withServerReason(describeApiError(error))
        : l.commonLoadErrorConnection;
