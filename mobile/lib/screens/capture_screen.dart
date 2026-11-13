import 'dart:io';

import 'package:material_ui/material_ui.dart';
import 'package:path/path.dart' as p;

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/services/camera_capture.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';

/// Camera capture screen — snap a photo or pick from gallery, then upload.
class CaptureScreen extends StatefulWidget {
  const CaptureScreen({super.key});

  @override
  State<CaptureScreen> createState() => _CaptureScreenState();
}

class _CaptureScreenState extends State<CaptureScreen> {
  File? _selectedFile;
  bool _uploading = false;
  String? _error;

  Future<void> _capture({bool fromCamera = true}) async {
    final file = await CameraCapture.pickImage(fromCamera: fromCamera);
    if (!mounted) return;
    if (file != null) {
      setState(() {
        _selectedFile = file;
        _error = null;
      });
    }
  }

  /// Pick a document file (PDF / PNG / JPG / TIFF) from the device's storage.
  Future<void> _pickDocument() async {
    final file = await CameraCapture.pickDocument();
    if (!mounted) return;
    if (file != null) {
      setState(() {
        _selectedFile = file;
        _error = null;
      });
    }
  }

  /// True when the selected file is a PDF — it can't render via [Image.file],
  /// so the preview shows a document placeholder instead of the bitmap.
  bool get _selectedIsPdf {
    final file = _selectedFile;
    if (file == null) return false;
    return p.extension(file.path).toLowerCase() == '.pdf';
  }

  Future<void> _upload() async {
    if (_selectedFile == null) return;

    // Resolve the localizations before the first await — context lookups after
    // an async gap are flagged by use_build_context_synchronously.
    final l = AppLocalizations.of(context);

    setState(() {
      _uploading = true;
      _error = null;
    });

    try {
      final result = await CameraCapture.uploadInvoice(_selectedFile!);
      // Refresh invoice list
      await InvoiceStore.instance.fetch();

      if (mounted) {
        final message =
            result['message'] as String? ?? l.captureUploadSuccess;
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(message)),
        );
        // Announce success before navigating away (WCAG 4.1.3).
        A11y.announce(context, message);
        Navigator.of(context).pop();
      }
    } catch (e) {
      final message = e is ApiException
          ? l.captureUploadFailedStatus(e.statusCode, e.message)
          : l.captureUploadFailed(e.toString());
      if (!mounted) return;
      setState(() {
        _uploading = false;
        _error = message;
      });
      A11y.announce(context, message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.captureTitle)),
      body: Column(
        children: [
          Expanded(
            child: _selectedFile != null
                ? (_selectedIsPdf
                      ? _buildPdfPreview(l)
                      : _buildImagePreview())
                : _buildEmptyState(l),
          ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: Semantics(
                liveRegion: true,
                child: Text(
                  _error!,
                  style: TextStyle(color: Colors.red.shade700),
                  textAlign: TextAlign.center,
                ),
              ),
            ),
          if (_selectedFile != null)
            SafeArea(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Row(
                  children: [
                    Expanded(
                      child: OutlinedButton.icon(
                        onPressed: _uploading ? null : _changeSource,
                        icon: const Icon(Icons.refresh),
                        label: Text(l.captureChange),
                      ),
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: FilledButton.icon(
                        onPressed: _uploading ? null : _upload,
                        icon: _uploading
                            ? const SizedBox(
                                width: 18,
                                height: 18,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                  color: Colors.white,
                                ),
                              )
                            : const Icon(Icons.upload),
                        label: Text(
                          _uploading ? l.captureUploading : l.captureUpload,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }

  /// Empty state — offers all three sources: camera, gallery, and a file
  /// picker for documents (PDF / PNG / JPG / TIFF).
  Widget _buildEmptyState(AppLocalizations l) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const ExcludeSemantics(
              child: Icon(
                Icons.camera_alt,
                size: 80,
                color: Colors.grey, // decorative placeholder
              ),
            ),
            const SizedBox(height: 16),
            Text(
              l.captureEmptyPrompt,
              style: const TextStyle(fontSize: 16),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 32),
            Wrap(
              alignment: WrapAlignment.center,
              spacing: 16,
              runSpacing: 12,
              children: [
                FilledButton.icon(
                  onPressed: () => _capture(fromCamera: true),
                  icon: const Icon(Icons.camera_alt),
                  label: Text(l.captureCamera),
                ),
                OutlinedButton.icon(
                  onPressed: () => _capture(fromCamera: false),
                  icon: const Icon(Icons.photo_library),
                  label: Text(l.captureGallery),
                ),
                OutlinedButton.icon(
                  onPressed: _pickDocument,
                  icon: const Icon(Icons.upload_file),
                  label: Text(l.captureChooseFile),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Text(
              l.captureSupportedFormats,
              style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
            ),
          ],
        ),
      ),
    );
  }

  /// Image preview for a picked photo / image file.
  Widget _buildImagePreview() {
    return InteractiveViewer(
      child: Image.file(
        _selectedFile!,
        fit: BoxFit.contain,
      ),
    );
  }

  /// Document placeholder preview for a picked PDF — a PDF can't render via
  /// [Image.file]. Shows the file name so the user can confirm their choice
  /// before uploading; the rendered PDF is viewable on the invoice detail
  /// screen after extraction.
  Widget _buildPdfPreview(AppLocalizations l) {
    final name = p.basename(_selectedFile!.path);
    return Center(
      child: Semantics(
        label: l.captureSelectedDocument(name),
        excludeSemantics: true,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.picture_as_pdf, size: 80, color: Colors.redAccent),
            const SizedBox(height: 16),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 32),
              child: Text(
                name,
                style: const TextStyle(fontSize: 16),
                textAlign: TextAlign.center,
              ),
            ),
            const SizedBox(height: 8),
            Text(l.capturePdfReady),
          ],
        ),
      ),
    );
  }

  /// Re-open a source chooser so the user can swap the selected file before
  /// uploading. Covers all three sources (camera / gallery / file).
  Future<void> _changeSource() async {
    final l = AppLocalizations.of(context);
    final choice = await showModalBottomSheet<String>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.camera_alt),
              title: Text(l.captureCamera),
              onTap: () => Navigator.pop(sheetContext, 'camera'),
            ),
            ListTile(
              leading: const Icon(Icons.photo_library),
              title: Text(l.captureGallery),
              onTap: () => Navigator.pop(sheetContext, 'gallery'),
            ),
            ListTile(
              leading: const Icon(Icons.upload_file),
              title: Text(l.captureChooseFile),
              onTap: () => Navigator.pop(sheetContext, 'file'),
            ),
          ],
        ),
      ),
    );
    switch (choice) {
      case 'camera':
        await _capture(fromCamera: true);
      case 'gallery':
        await _capture(fromCamera: false);
      case 'file':
        await _pickDocument();
    }
  }
}
