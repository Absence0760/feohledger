import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/workflow.dart';
import 'package:feohledger_mobile/screens/workflows_screen.dart' show WorkflowStatusBadge;

/// Read-only detail for one workflow definition — its configured steps (type,
/// name, enabled flag) and a brief per-step config summary. No editing: the
/// no-code builder stays on the web. Fetches a fresh copy of the definition on
/// open via `GET /api/workflows/{id}` so it reflects the latest config.
class WorkflowDetailScreen extends StatefulWidget {
  final String workflowId;

  const WorkflowDetailScreen({super.key, required this.workflowId});

  @override
  State<WorkflowDetailScreen> createState() => _WorkflowDetailScreenState();
}

class _WorkflowDetailScreenState extends State<WorkflowDetailScreen> {
  WorkflowDefinition? _workflow;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final wf = await WorkflowApi.getById(widget.workflowId);
      if (!mounted) return;
      setState(() {
        _workflow = wf;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.toString();
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar:
          AppBar(title: Text(_workflow?.name ?? l.workflowDetailFallbackTitle)),
      body: _buildBody(l),
    );
  }

  Widget _buildBody(AppLocalizations l) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null || _workflow == null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.error_outline, size: 48, color: Colors.red.shade700),
            const SizedBox(height: 12),
            Text(l.workflowDetailLoadError),
            const SizedBox(height: 12),
            FilledButton(onPressed: _load, child: Text(l.commonRetry)),
          ],
        ),
      );
    }

    final wf = _workflow!;
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        children: [
          _Header(workflow: wf),
          const Divider(height: 1),
          if (wf.steps.isEmpty)
            Padding(
              padding: const EdgeInsets.all(24),
              child: Center(child: Text(l.workflowDetailNoSteps)),
            )
          else
            for (final step in wf.steps) _StepTile(step: step),
        ],
      ),
    );
  }
}

class _Header extends StatelessWidget {
  final WorkflowDefinition workflow;

  const _Header({required this.workflow});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  workflow.name,
                  style: const TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              WorkflowStatusBadge(isActive: workflow.isActive),
            ],
          ),
          if (workflow.isDefault)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                AppLocalizations.of(context).workflowDetailDefaultWorkflow,
                style: TextStyle(color: Colors.blue.shade800, fontSize: 13),
              ),
            ),
          if (workflow.description != null &&
              workflow.description!.isNotEmpty) ...[
            const SizedBox(height: 8),
            Text(
              workflow.description!,
              style: TextStyle(color: Colors.grey.shade700),
            ),
          ],
        ],
      ),
    );
  }
}

class _StepTile extends StatelessWidget {
  final WorkflowStepConfig step;

  const _StepTile({required this.step});

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final summary = _configSummary(step, l);
    // One merged announcement per step row.
    final semanticsLabel = [
      l.workflowDetailStepNumber(step.number),
      step.typeLabel,
      step.name,
      step.enabled
          ? l.workflowDetailStepEnabled
          : l.workflowDetailStepDisabled,
      ?summary,
    ].join(', ');

    return Semantics(
      label: semanticsLabel,
      excludeSemantics: true,
      child: ListTile(
        leading: CircleAvatar(
          radius: 16,
          backgroundColor: step.enabled
              ? Colors.blue.withValues(alpha: 0.15)
              : Colors.grey.withValues(alpha: 0.15),
          child: Text(
            '${step.number}',
            style: TextStyle(
              fontSize: 13,
              fontWeight: FontWeight.w600,
              color: step.enabled ? Colors.blue.shade800 : Colors.grey.shade700,
            ),
          ),
        ),
        title: Text(step.name.isEmpty ? step.typeLabel : step.name),
        subtitle: Text(
          [
            step.typeLabel,
            ?summary,
          ].join(' · '),
        ),
        trailing: step.enabled
            ? null
            : Text(
                l.workflowDetailStepDisabled,
                style: TextStyle(
                  color: Colors.grey.shade700,
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
      ),
    );
  }

  /// A short, PII-free summary of the common config keys for a step type.
  /// Returns null when there's nothing useful to show.
  static String? _configSummary(WorkflowStepConfig step, AppLocalizations l) {
    final cfg = step.config;
    if (cfg.isEmpty) return null;
    switch (step.type) {
      case 'approval':
        final strategy = cfg['approver_strategy'];
        final ids = cfg['approver_ids'];
        final count = ids is List ? ids.length : 0;
        if (strategy is String && strategy.isNotEmpty) {
          return count > 0
              ? '$strategy · ${l.workflowDetailApproverCount(count)}'
              : strategy;
        }
        return null;
      case 'delay':
        final hours = cfg['delay_hours'] ?? cfg['hours'];
        return hours != null
            ? l.workflowDetailDelaySummary(hours.toString())
            : null;
      case 'condition':
        final field = cfg['field'];
        return field is String && field.isNotEmpty
            ? l.workflowDetailConditionSummary(field)
            : null;
      default:
        return null;
    }
  }
}
