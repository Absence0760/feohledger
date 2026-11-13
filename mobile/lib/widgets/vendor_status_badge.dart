import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/models/vendor.dart';

class VendorStatusBadge extends StatelessWidget {
  final VendorStatus status;

  const VendorStatusBadge({super.key, required this.status});

  Color get _color => switch (status) {
    VendorStatus.active => Colors.green,
    VendorStatus.unverified => Colors.orange,
    VendorStatus.inactive => Colors.grey,
    VendorStatus.rejected => Colors.red,
  };

  // Darkened foreground for AA contrast (≥4.5:1) against the pale tint
  // background — same rationale as ContractStatusBadge. Orange can't reach
  // 4.5:1 as a true orange, so `unverified` uses a dark brown (deep amber).
  Color get _textColor => switch (status) {
    VendorStatus.active => Colors.green.shade900,
    VendorStatus.unverified => Colors.brown.shade800,
    VendorStatus.inactive => Colors.grey.shade800,
    VendorStatus.rejected => Colors.red.shade900,
  };

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: 'Status: ${status.label}',
      excludeSemantics: true,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: _color.withValues(alpha: 0.15),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: _color.withValues(alpha: 0.4)),
        ),
        child: Text(
          status.label,
          style: TextStyle(
            color: _textColor,
            fontSize: 12,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
    );
  }
}
