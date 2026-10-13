import 'dart:convert';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:http_parser/http_parser.dart';
import 'package:image_picker/image_picker.dart';
import 'package:path/path.dart' as p;

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/config.dart';

/// Camera capture and invoice upload service.
class CameraCapture {
  static final _picker = ImagePicker();

  /// Document file extensions the backend extraction pipeline accepts. Mirrors
  /// the server's `ALLOWED_CONTENT_TYPES` (PDF / PNG / JPEG / TIFF) — the
  /// picker rejects anything else up-front so the upload can't 422 on type.
  static const documentExtensions = <String>[
    'pdf',
    'png',
    'jpg',
    'jpeg',
    'tiff',
    'tif',
  ];

  /// Pick an image from camera or gallery.
  /// Returns the file, or null if cancelled.
  static Future<File?> pickImage({bool fromCamera = true}) async {
    try {
      final image = await _picker.pickImage(
        source: fromCamera ? ImageSource.camera : ImageSource.gallery,
        maxWidth: 2048,
        maxHeight: 2048,
        imageQuality: 85,
      );
      if (image == null) return null;
      return File(image.path);
    } catch (e) {
      debugPrint('[camera] Pick image failed: $e');
      return null;
    }
  }

  /// Pick a document file (PDF / PNG / JPG / JPEG / TIFF) from the device's
  /// file system. Returns the file, or null if the user cancelled. Runs the
  /// same upload path as the camera capture, so the backend extraction pipeline
  /// treats both identically.
  static Future<File?> pickDocument() async {
    try {
      // Single-select and never buffered: we upload by path. (`pickFiles`
      // defaults to multi-select from file_picker 12.)
      final picked = await FilePicker.pickFile(
        type: FileType.custom,
        allowedExtensions: documentExtensions,
      );
      final path = picked?.path;
      if (path == null) return null;
      return File(path);
    } catch (e) {
      debugPrint('[camera] Pick document failed: $e');
      return null;
    }
  }

  /// Upload an invoice file (image) to the backend.
  /// Returns the response data with invoice id, status, and message.
  static Future<Map<String, dynamic>> uploadInvoice(File file) async {
    final api = ApiClient();
    final uri = Uri.parse('${AppConfig.apiUrl}/invoices/upload');
    final filename = p.basename(file.path);

    debugPrint('[camera] Uploading $filename to $uri');

    final request = http.MultipartRequest('POST', uri);
    request.headers.addAll(api.authHeaders);
    // Determine MIME type from extension — image_picker often omits it
    final ext = p.extension(filename).toLowerCase();
    final contentType = switch (ext) {
      '.jpg' || '.jpeg' => MediaType('image', 'jpeg'),
      '.png' => MediaType('image', 'png'),
      '.tiff' || '.tif' => MediaType('image', 'tiff'),
      '.pdf' => MediaType('application', 'pdf'),
      _ => MediaType('image', 'jpeg'), // camera photos default to JPEG
    };

    debugPrint('[camera] File: $filename, content-type: $contentType');

    request.files.add(
      await http.MultipartFile.fromPath(
        'file',
        file.path,
        filename: filename,
        contentType: contentType,
      ),
    );

    final streamedResponse =
        await request.send().timeout(const Duration(seconds: 30));
    final response = await http.Response.fromStream(streamedResponse);

    debugPrint('[camera] Upload response: ${response.statusCode}');

    if (response.statusCode == 401) {
      await api.clearSession();
      throw ApiException(401, 'Unauthorized');
    }
    if (response.statusCode >= 400) {
      throw ApiException(response.statusCode, response.body);
    }

    if (response.body.isNotEmpty) {
      return jsonDecode(response.body) as Map<String, dynamic>;
    }
    return {};
  }
}
