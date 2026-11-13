import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/models/contract.dart';

class ContractStatusBadge extends StatelessWidget {
  final ContractStatus status;

  const ContractStatusBadge({super.key, required this.status});

  Color get _color => switch (status) {
    ContractStatus.draft => Colors.blueGrey,
    ContractStatus.active => Colors.green,
    ContractStatus.expired => Colors.orange,
    ContractStatus.terminated => Colors.red,
    ContractStatus.cancelled => Colors.grey,
  };

  // Darkened foreground for AA contrast (≥4.5:1) against the pale tint
  // background. 12px bold text → the 4.5:1 bar applies; orange can't reach it
  // as a true orange, so `expired` uses a dark brown that reads as deep amber.
  Color get _textColor => switch (status) {
    ContractStatus.draft => Colors.blueGrey.shade800, // 8.10
    ContractStatus.active => Colors.green.shade900, // 6.84
    ContractStatus.expired => Colors.brown.shade800, // 10.09
    ContractStatus.terminated => Colors.red.shade900, // 5.40
    ContractStatus.cancelled => Colors.grey.shade800,
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
