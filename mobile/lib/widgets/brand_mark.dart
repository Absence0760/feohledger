import 'package:flutter/widgets.dart';

/// The FeohLedger mark: feoh (ᚠ) written as a split tally stick on its navy
/// tile (docs/decisions.md §171).
///
/// Always decorative, so it is excluded from semantics. On the login screen the
/// app name is spelled out right below it and announcing both would say it
/// twice; on the splash screen it is a transient placeholder with nothing to
/// act on.
///
/// The PNGs under `assets/brand/` (1x, 2.0x, 3.0x) are rendered from
/// `assets/logo-mark.svg` by `assets/gen-icons.sh`; never edit them by hand.
class BrandMark extends StatelessWidget {
  const BrandMark({super.key, this.size = 64});

  /// Side length in logical pixels. The bundled variants are cut for 64.
  final double size;

  static const assetPath = 'assets/brand/logo_mark.png';

  @override
  Widget build(BuildContext context) {
    return ExcludeSemantics(
      child: Image.asset(
        assetPath,
        width: size,
        height: size,
        filterQuality: FilterQuality.medium,
      ),
    );
  }
}
