import 'package:material_ui/material_ui.dart';
import 'package:url_launcher/url_launcher.dart';

/// Opens [uri] outside the app; true when a handler took it.
typedef UrlOpener = Future<bool> Function(Uri uri);

/// The default [UrlOpener]: the platform browser (or the ERP's own app, when
/// it claims the URL), never an in-app web view.
Future<bool> openExternally(Uri uri) =>
    launchUrl(uri, mode: LaunchMode.externalApplication);

/// Text that opens a backend-supplied URL outside the app — e.g. an invoice's
/// `erp_document_url` (backend `erp_adapters/deep_links.py`, decisions §354).
///
/// Only an absolute `https` URL becomes a link; anything else (null, another
/// scheme, a relative path) renders as plain text, exactly as a value with no
/// URL does, so a bad value can never launch something unexpected.
///
/// Semantics: one node with the link role, announced as "[text], [hint]".
/// The tap area is at least 48×48 (WCAG 2.5.8, Android / iOS target size).
class ExternalLink extends StatelessWidget {
  final String text;
  final String? url;

  /// Appended to [text] in the accessible name ("opens the bill in the ERP"),
  /// the equivalent of the web's visually-hidden suffix.
  final String hint;

  /// Shown in a snackbar when no app could open the URL.
  final String failureMessage;
  final TextStyle? style;

  /// Injected by tests; defaults to [openExternally].
  final UrlOpener? opener;

  const ExternalLink({
    super.key,
    required this.text,
    required this.url,
    required this.hint,
    required this.failureMessage,
    this.style,
    this.opener,
  });

  /// [url] as a launchable [Uri], or null when it must stay plain text.
  static Uri? safeUri(String? url) {
    if (url == null || url.isEmpty) return null;
    final uri = Uri.tryParse(url);
    if (uri == null || uri.scheme != 'https' || uri.host.isEmpty) return null;
    return uri;
  }

  Future<void> _open(BuildContext context, Uri uri) async {
    final messenger = ScaffoldMessenger.maybeOf(context);
    bool opened;
    try {
      opened = await (opener ?? openExternally)(uri);
    } catch (_) {
      opened = false;
    }
    if (!opened) {
      messenger?.showSnackBar(SnackBar(content: Text(failureMessage)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final uri = safeUri(url);
    if (uri == null) return Text(text, style: style);
    final color = Colors.indigo.shade700;
    return Semantics(
      link: true,
      label: '$text, $hint',
      excludeSemantics: true,
      onTap: () => _open(context, uri),
      child: InkWell(
        onTap: () => _open(context, uri),
        borderRadius: BorderRadius.circular(4),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 48, minWidth: 48),
          child: Align(
            alignment: Alignment.centerLeft,
            widthFactor: 1,
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Flexible(
                  child: Text(
                    text,
                    style: (style ?? const TextStyle()).copyWith(
                      color: color,
                      decoration: TextDecoration.underline,
                      decorationColor: color,
                    ),
                  ),
                ),
                const SizedBox(width: 4),
                Icon(Icons.open_in_new, size: 14, color: color),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
