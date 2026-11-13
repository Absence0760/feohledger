import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/admin_user.dart';
import 'package:feohledger_mobile/stores/admin_user_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/utils/debouncer.dart';

/// Admin — user management. Lists the org's users (search by name/email) and
/// lets an admin change a user's roles or activate / deactivate them over
/// `PATCH /api/admin/users/{id}`. Admin-only (the route is
/// `require_roles(ROLE_ADMIN)`); the Settings entry point is hidden for
/// everyone else, and this screen guards again on build.
class AdminUsersScreen extends StatefulWidget {
  const AdminUsersScreen({super.key});

  @override
  State<AdminUsersScreen> createState() => _AdminUsersScreenState();
}

class _AdminUsersScreenState extends State<AdminUsersScreen> {
  final _searchController = TextEditingController();
  final _searchDebouncer = Debouncer();

  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      AdminUserStore.instance.fetch();
    });
  }

  @override
  void dispose() {
    _searchDebouncer.cancel();
    _searchController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _createUser,
        icon: const Icon(Icons.person_add_alt_1),
        label: Text(l.adminUsersCreateUser),
      ),
      appBar: AppBar(
        title: Text(l.adminUsersTitle),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(56),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
            child: SearchBar(
              controller: _searchController,
              hintText: l.adminUsersSearchHint,
              leading: const Icon(Icons.search, size: 20),
              onChanged: (q) => _searchDebouncer.run(
                () => AdminUserStore.instance.setSearch(q),
              ),
              elevation: WidgetStateProperty.all(0),
            ),
          ),
        ),
      ),
      body: ListenableBuilder(
        listenable: AdminUserStore.instance,
        builder: (context, _) {
          final store = AdminUserStore.instance;

          if (store.loading && store.users.isEmpty) {
            return const Center(child: CircularProgressIndicator());
          }
          if (store.error != null && store.users.isEmpty) {
            return _ErrorState(message: store.error!, onRetry: store.fetch);
          }
          if (store.users.isEmpty) {
            return Center(child: Text(l.adminUsersEmpty));
          }

          return RefreshIndicator(
            onRefresh: store.fetch,
            child: ListView.separated(
              itemCount: store.users.length,
              separatorBuilder: (_, _) => const Divider(height: 1),
              itemBuilder: (context, index) {
                final user = store.users[index];
                return _UserTile(
                  user: user,
                  onTap: () => _showActions(user),
                );
              },
            ),
          );
        },
      ),
    );
  }

  void _showActions(AdminUser user) {
    final isSelf = AuthStore.instance.user?.id == user.id;
    final l = AppLocalizations.of(context);
    showModalBottomSheet<void>(
      context: context,
      builder: (sheetContext) {
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ListTile(
                title: Text(
                  user.fullName,
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
                subtitle: Text(user.email),
              ),
              const Divider(height: 1),
              ListTile(
                leading: const Icon(Icons.badge_outlined),
                title: Text(l.adminUsersEditRoles),
                subtitle: Text(
                  user.roles.isEmpty
                      ? l.adminUsersNoRoles
                      : user.roles.join(', '),
                ),
                onTap: () {
                  Navigator.of(sheetContext).pop();
                  _editRoles(user);
                },
              ),
              ListTile(
                leading: Icon(
                  user.isActive ? Icons.block : Icons.check_circle_outline,
                  color: user.isActive ? Colors.red.shade700 : Colors.green,
                ),
                title: Text(
                  user.isActive
                      ? l.adminUsersDeactivate
                      : l.adminUsersActivate,
                ),
                subtitle: isSelf
                    ? Text(l.adminUsersCannotDeactivateSelf)
                    : Text(
                        user.isActive
                            ? l.adminUsersDeactivateHint
                            : l.adminUsersActivateHint,
                      ),
                // The backend lets an admin deactivate anyone, but locking
                // yourself out would be a footgun — disable it for self.
                enabled: !(isSelf && user.isActive),
                onTap: () {
                  Navigator.of(sheetContext).pop();
                  _toggleActive(user);
                },
              ),
              ListTile(
                leading: Icon(
                  Icons.delete_outline,
                  color: isSelf ? null : Colors.red.shade700,
                ),
                title: Text(l.adminUsersDelete),
                subtitle: isSelf
                    ? Text(l.adminUsersCannotDeleteSelf)
                    : Text(l.adminUsersDeleteHint),
                // The backend 409s on self-delete; disable it here so the
                // admin can't lock themselves out of their own account.
                enabled: !isSelf,
                onTap: () {
                  Navigator.of(sheetContext).pop();
                  _deleteUser(user);
                },
              ),
            ],
          ),
        );
      },
    );
  }

  Future<void> _editRoles(AdminUser user) async {
    final l = AppLocalizations.of(context);
    final available = AdminUserStore.instance.systemRoleNames;
    final selected = await showModalBottomSheet<List<String>>(
      context: context,
      isScrollControlled: true,
      builder: (sheetContext) => _RoleEditor(
        available: available,
        initial: user.roles.where(available.contains).toList(),
      ),
    );
    if (selected == null || !mounted) return;

    final ok = await AdminUserStore.instance.setRoles(user.id, selected);
    if (!mounted) return;
    final message = ok
        ? l.adminUsersRolesUpdated(user.fullName)
        : l.adminUsersRolesUpdateFailed(AdminUserStore.instance.error ?? '');
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
    A11y.announce(context, message);
  }

  Future<void> _toggleActive(AdminUser user) async {
    final l = AppLocalizations.of(context);
    final target = !user.isActive;
    final ok = await AdminUserStore.instance.setActive(user.id, target);
    if (!mounted) return;
    final message = ok
        ? (target
            ? l.adminUsersActivated(user.fullName)
            : l.adminUsersDeactivated(user.fullName))
        : l.adminUsersUpdateFailed(
            user.fullName, AdminUserStore.instance.error ?? '');
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
    A11y.announce(context, message);
  }

  Future<void> _createUser() async {
    final l = AppLocalizations.of(context);
    final available = AdminUserStore.instance.systemRoleNames;
    final draft = await showModalBottomSheet<_NewUserDraft>(
      context: context,
      isScrollControlled: true,
      builder: (sheetContext) => _CreateUserSheet(availableRoles: available),
    );
    if (draft == null || !mounted) return;

    final result = await AdminUserStore.instance.createUser(
      email: draft.email,
      fullName: draft.fullName,
      roleNames: draft.roleNames,
    );
    if (!mounted) return;

    if (result == null) {
      final message =
          l.adminUsersCreateFailed(AdminUserStore.instance.error ?? '');
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(message)));
      A11y.announce(context, message);
      return;
    }

    final message = l.adminUsersCreated(result.user.fullName);
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
    A11y.announce(context, message);
    // Surface the one-time temp password so the admin can hand it over.
    await _showTempPassword(result);
  }

  Future<void> _showTempPassword(CreateUserResult result) async {
    final l = AppLocalizations.of(context);
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(l.adminUsersTempPasswordTitle),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(l.adminUsersTempPasswordBody(result.user.fullName)),
            const SizedBox(height: 16),
            Container(
              width: double.infinity,
              padding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              decoration: BoxDecoration(
                color: Theme.of(dialogContext).colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(8),
              ),
              child: SelectableText(
                result.temporaryPassword,
                style: const TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ],
        ),
        actions: [
          FilledButton(
            onPressed: () => Navigator.of(dialogContext).pop(),
            child: Text(AppLocalizations.of(dialogContext).commonClose),
          ),
        ],
      ),
    );
  }

  Future<void> _deleteUser(AdminUser user) async {
    final l = AppLocalizations.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(l.adminUsersDeleteConfirmTitle(user.fullName)),
        content: Text(
          l.adminUsersDeleteConfirmBody(user.fullName, user.email),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(false),
            child: Text(l.commonCancel),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: Colors.red.shade700,
            ),
            onPressed: () => Navigator.of(dialogContext).pop(true),
            child: Text(l.adminUsersDelete),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    final ok = await AdminUserStore.instance.deleteUser(user.id);
    if (!mounted) return;
    final message = ok
        ? l.adminUsersDeleted(user.fullName)
        : l.adminUsersDeleteFailed(
            user.fullName, AdminUserStore.instance.error ?? '');
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
    A11y.announce(context, message);
  }
}

/// The validated output of [_CreateUserSheet] — a new user's details ready to
/// POST. Returned via `Navigator.pop` (Create) or null (Cancel / dismiss).
class _NewUserDraft {
  final String email;
  final String fullName;
  final List<String> roleNames;

  const _NewUserDraft({
    required this.email,
    required this.fullName,
    required this.roleNames,
  });
}

/// Create-user form sheet — full name + email (validated) + a checkbox per
/// available system role. Returns a [_NewUserDraft] on Create, or null on
/// Cancel / dismiss. Mirrors the `_RoleEditor` shape + the backend
/// `CreateUserRequest` (email + full_name + role_names; the temp password is
/// server-generated).
class _CreateUserSheet extends StatefulWidget {
  final List<String> availableRoles;

  const _CreateUserSheet({required this.availableRoles});

  @override
  State<_CreateUserSheet> createState() => _CreateUserSheetState();
}

class _CreateUserSheetState extends State<_CreateUserSheet> {
  final _formKey = GlobalKey<FormState>();
  final _nameController = TextEditingController();
  final _emailController = TextEditingController();
  late final Set<String> _selectedRoles = {};

  // Pragmatic email shape check (a backend re-validates). Mirrors the spirit of
  // the web client's check — not a full RFC 5322 parser.
  static final _emailRegExp = RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$');

  @override
  void dispose() {
    _nameController.dispose();
    _emailController.dispose();
    super.dispose();
  }

  void _submit() {
    if (!(_formKey.currentState?.validate() ?? false)) return;
    Navigator.of(context).pop(
      _NewUserDraft(
        email: _emailController.text.trim(),
        fullName: _nameController.text.trim(),
        roleNames: _selectedRoles.toList(),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    // Lift the sheet above the keyboard.
    final bottomInset = MediaQuery.of(context).viewInsets.bottom;
    return Padding(
      padding: EdgeInsets.only(bottom: bottomInset),
      child: SafeArea(
        child: SingleChildScrollView(
          child: Form(
            key: _formKey,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.all(16),
                  child: Text(
                    l.adminUsersCreateTitle,
                    style: const TextStyle(
                      fontWeight: FontWeight.w600,
                      fontSize: 16,
                    ),
                  ),
                ),
                const Divider(height: 1),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 16, 16, 0),
                  child: TextFormField(
                    controller: _nameController,
                    textInputAction: TextInputAction.next,
                    decoration: InputDecoration(
                      labelText: l.adminUsersFieldFullName,
                    ),
                    validator: (v) => (v == null || v.trim().isEmpty)
                        ? l.adminUsersValidationNameRequired
                        : null,
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
                  child: TextFormField(
                    controller: _emailController,
                    keyboardType: TextInputType.emailAddress,
                    autocorrect: false,
                    decoration: InputDecoration(
                      labelText: l.adminUsersFieldEmail,
                    ),
                    validator: (v) {
                      final value = v?.trim() ?? '';
                      if (value.isEmpty) {
                        return l.adminUsersValidationEmailRequired;
                      }
                      if (!_emailRegExp.hasMatch(value)) {
                        return l.adminUsersValidationEmailInvalid;
                      }
                      return null;
                    },
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
                  child: Text(
                    l.adminUsersFieldRoles,
                    style: TextStyle(
                      color: Colors.grey.shade700,
                      fontSize: 13,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
                for (final role in widget.availableRoles)
                  CheckboxListTile(
                    value: _selectedRoles.contains(role),
                    title: Text(role),
                    onChanged: (checked) => setState(() {
                      if (checked == true) {
                        _selectedRoles.add(role);
                      } else {
                        _selectedRoles.remove(role);
                      }
                    }),
                  ),
                const Divider(height: 1),
                Padding(
                  padding: const EdgeInsets.all(12),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      TextButton(
                        onPressed: () => Navigator.of(context).pop(),
                        child: Text(l.commonCancel),
                      ),
                      const SizedBox(width: 8),
                      FilledButton(
                        onPressed: _submit,
                        child: Text(l.adminUsersCreateSubmit),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Multi-select role editor — a checkbox per available system role with an
/// Apply / Cancel footer. Returns the chosen role-name list (Apply) or null
/// (Cancel / dismiss).
class _RoleEditor extends StatefulWidget {
  final List<String> available;
  final List<String> initial;

  const _RoleEditor({required this.available, required this.initial});

  @override
  State<_RoleEditor> createState() => _RoleEditorState();
}

class _RoleEditorState extends State<_RoleEditor> {
  late final Set<String> _selected = {...widget.initial};

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.all(16),
            child: Text(
              l.adminUsersEditRoles,
              style: const TextStyle(
                fontWeight: FontWeight.w600,
                fontSize: 16,
              ),
            ),
          ),
          const Divider(height: 1),
          for (final role in widget.available)
            CheckboxListTile(
              value: _selected.contains(role),
              title: Text(role),
              onChanged: (checked) => setState(() {
                if (checked == true) {
                  _selected.add(role);
                } else {
                  _selected.remove(role);
                }
              }),
            ),
          const Divider(height: 1),
          Padding(
            padding: const EdgeInsets.all(12),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(
                  onPressed: () => Navigator.of(context).pop(),
                  child: Text(l.commonCancel),
                ),
                const SizedBox(width: 8),
                FilledButton(
                  onPressed: () =>
                      Navigator.of(context).pop(_selected.toList()),
                  child: Text(l.commonApply),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _UserTile extends StatelessWidget {
  final AdminUser user;
  final VoidCallback onTap;

  const _UserTile({required this.user, required this.onTap});

  String _semanticLabel(AppLocalizations l) {
    final parts = <String>[
      user.fullName,
      user.email,
      if (user.roles.isNotEmpty) 'roles ${user.roles.join(', ')}',
      user.isActive ? l.adminUsersRoleActive : l.adminUsersRoleInactive,
    ];
    return parts.join(', ');
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Semantics(
      label: _semanticLabel(l),
      button: true,
      excludeSemantics: true,
      child: ListTile(
        onTap: onTap,
        leading: CircleAvatar(
          backgroundColor: user.isActive ? Colors.blue : Colors.grey.shade500,
          child: Text(
            user.initial,
            style: const TextStyle(color: Colors.white),
          ),
        ),
        title: Text(
          user.fullName,
          style: const TextStyle(fontWeight: FontWeight.w600),
          overflow: TextOverflow.ellipsis,
        ),
        subtitle: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              user.email,
              style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
              overflow: TextOverflow.ellipsis,
            ),
            const SizedBox(height: 4),
            Wrap(
              spacing: 6,
              runSpacing: 4,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                if (!user.isActive)
                  _InactiveBadge(),
                ...user.roles.map(
                  (r) => Chip(
                    label: Text(r, style: const TextStyle(fontSize: 11)),
                    visualDensity: VisualDensity.compact,
                    materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    padding: EdgeInsets.zero,
                  ),
                ),
              ],
            ),
          ],
        ),
        trailing: const Icon(Icons.chevron_right),
        isThreeLine: true,
      ),
    );
  }
}

class _InactiveBadge extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    // shade100 tint + shade900 text clears AA contrast at small sizes.
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: Colors.red.shade100,
        borderRadius: BorderRadius.circular(6),
      ),
      child: Text(
        AppLocalizations.of(context).adminUsersInactiveBadge,
        style: TextStyle(
          color: Colors.red.shade900,
          fontSize: 11,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

class _ErrorState extends StatelessWidget {
  final String message;
  final Future<void> Function() onRetry;

  const _ErrorState({required this.message, required this.onRetry});

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.error_outline, size: 48, color: Colors.red.shade700),
          const SizedBox(height: 12),
          Text(l.adminUsersLoadError),
          const SizedBox(height: 12),
          FilledButton(onPressed: onRetry, child: Text(l.commonRetry)),
        ],
      ),
    );
  }
}
