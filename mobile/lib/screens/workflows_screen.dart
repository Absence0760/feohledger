import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/workflow.dart';
import 'package:feohledger_mobile/screens/workflow_detail_screen.dart';
import 'package:feohledger_mobile/stores/workflow_store.dart';

/// Admin — read-only workflow management. Lists the org's workflow definitions
/// (name, active/default status, step count) over `GET /api/workflows`; tapping
/// a row opens a read-only step view. The no-code builder (create / edit /
/// versions / simulate) stays on the web — this is a phone-friendly viewer.
/// Admin-gated: the Settings entry point is hidden for non-admins (mirrors the
/// web nav `roles: ['admin']`); reads are open on the backend, so this is a UI
/// gate, not a security boundary.
class WorkflowsScreen extends StatefulWidget {
  const WorkflowsScreen({super.key});

  @override
  State<WorkflowsScreen> createState() => _WorkflowsScreenState();
}

class _WorkflowsScreenState extends State<WorkflowsScreen> {
  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      WorkflowStore.instance.fetch();
    });
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.workflowsTitle)),
      body: ListenableBuilder(
        listenable: WorkflowStore.instance,
        builder: (context, _) {
          final store = WorkflowStore.instance;

          if (store.loading && store.workflows.isEmpty) {
            return const Center(child: CircularProgressIndicator());
          }
          if (store.error != null && store.workflows.isEmpty) {
            return _ErrorState(onRetry: store.fetch);
          }
          if (store.workflows.isEmpty) {
            return Center(child: Text(l.workflowsEmpty));
          }

          return RefreshIndicator(
            onRefresh: store.fetch,
            child: ListView.separated(
              itemCount: store.workflows.length,
              separatorBuilder: (_, _) => const Divider(height: 1),
              itemBuilder: (context, index) {
                final wf = store.workflows[index];
                return _WorkflowTile(
                  workflow: wf,
                  onTap: () => Navigator.of(context).push(
                    MaterialPageRoute(
                      builder: (_) => WorkflowDetailScreen(workflowId: wf.id),
                    ),
                  ),
                );
              },
            ),
          );
        },
      ),
    );
  }
}

class _WorkflowTile extends StatelessWidget {
  final WorkflowDefinition workflow;
  final VoidCallback onTap;

  const _WorkflowTile({required this.workflow, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final stepCount = workflow.steps.length;
    final stepLabel = l.workflowsStepCount(stepCount);
    final statusText =
        workflow.isActive ? l.workflowsStatusActive : l.workflowsStatusInactive;

    // One merged announcement per row so assistive tech reads a single phrase.
    final semanticsLabel = [
      workflow.name,
      if (workflow.isDefault) l.workflowsDefault,
      statusText,
      stepLabel,
    ].join(', ');

    return Semantics(
      label: semanticsLabel,
      button: true,
      excludeSemantics: true,
      child: ListTile(
        leading: const Icon(Icons.account_tree_outlined),
        title: Text(workflow.name),
        subtitle: Text(stepLabel),
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (workflow.isDefault) ...[
              _Pill(label: l.workflowsDefault, color: Colors.blue),
              const SizedBox(width: 6),
            ],
            WorkflowStatusBadge(isActive: workflow.isActive),
          ],
        ),
        onTap: onTap,
      ),
    );
  }
}

/// Active / inactive status chip. Text rendered in a darkened accent variant
/// (`.shade700`/`.shade800`) over a 0.15-alpha tint so it clears WCAG AA
/// contrast — the same convention as the other mobile status badges.
class WorkflowStatusBadge extends StatelessWidget {
  final bool isActive;

  const WorkflowStatusBadge({super.key, required this.isActive});

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final color = isActive ? Colors.green : Colors.grey;
    final fg = isActive ? Colors.green.shade800 : Colors.grey.shade700;
    final label =
        isActive ? l.workflowsStatusActive : l.workflowsStatusInactive;
    return Semantics(
      label: '${l.invoicesColStatus}: $label',
      excludeSemantics: true,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: color.withValues(alpha: 0.15),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Text(
          label,
          style: TextStyle(color: fg, fontSize: 12, fontWeight: FontWeight.w600),
        ),
      ),
    );
  }
}

class _Pill extends StatelessWidget {
  final String label;
  final MaterialColor color;

  const _Pill({required this.label, required this.color});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: color.shade800,
          fontSize: 12,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

class _ErrorState extends StatelessWidget {
  final Future<void> Function() onRetry;

  const _ErrorState({required this.onRetry});

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.error_outline, size: 48, color: Colors.red.shade700),
          const SizedBox(height: 12),
          Text(l.workflowsLoadError),
          const SizedBox(height: 12),
          FilledButton(onPressed: onRetry, child: Text(l.commonRetry)),
        ],
      ),
    );
  }
}
