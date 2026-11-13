import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/models/vendor.dart';
import 'package:feohledger_mobile/widgets/vendor_status_badge.dart';

class VendorListTile extends StatelessWidget {
  final Vendor vendor;
  final VoidCallback? onTap;

  const VendorListTile({super.key, required this.vendor, this.onTap});

  // One sensible screen-reader announcement for the whole row instead of
  // walking each disjoint span (WCAG 1.3.1 / 4.1.2).
  String get _semanticLabel {
    final parts = <String>[
      vendor.name,
      if (vendor.code != null && vendor.code!.isNotEmpty) 'code ${vendor.code}',
      vendor.status.label,
      '${vendor.invoiceCount} '
          '${vendor.invoiceCount == 1 ? 'invoice' : 'invoices'}',
      'source ${vendor.sourceLabel}',
    ];
    return parts.join(', ');
  }

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: _semanticLabel,
      button: onTap != null,
      excludeSemantics: true,
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
        onTap: onTap,
        title: Row(
          children: [
            Expanded(
              child: Text(
                vendor.name,
                style: const TextStyle(fontWeight: FontWeight.w600),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            VendorStatusBadge(status: vendor.status),
          ],
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Row(
            children: [
              if (vendor.code != null && vendor.code!.isNotEmpty) ...[
                Text(
                  vendor.code!,
                  style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
                ),
                const SizedBox(width: 12),
              ],
              Flexible(
                child: Text(
                  vendor.email ?? vendor.sourceLabel,
                  style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              const Spacer(),
              Text(
                '${vendor.invoiceCount} inv',
                style: TextStyle(color: Colors.grey.shade700, fontSize: 12),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
