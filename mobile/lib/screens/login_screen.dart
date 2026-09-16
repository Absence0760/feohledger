import 'package:flutter/material.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/mfa_screen.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/widgets/brand_mark.dart';

class LoginScreen extends StatefulWidget {
  /// Called once the user is fully signed in (password-only or after the MFA
  /// second factor). The screen does NOT navigate itself — the root [AuthGate]
  /// renders the home screen off `AuthStore.loggedIn`; this callback only tells
  /// the gate to release a biometric lock left over from a restored session.
  final VoidCallback? onSignedIn;

  const LoginScreen({super.key, this.onSignedIn});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _emailController = TextEditingController();
  final _passwordController = TextEditingController();
  final _tenantController = TextEditingController(text: 'acme');
  final _formKey = GlobalKey<FormState>();
  bool _obscurePassword = true;

  @override
  void dispose() {
    _emailController.dispose();
    _passwordController.dispose();
    _tenantController.dispose();
    super.dispose();
  }

  Future<void> _login() async {
    if (!_formKey.currentState!.validate()) return;

    final result = await AuthStore.instance.login(
      _emailController.text.trim(),
      _passwordController.text,
      _tenantController.text.trim(),
    );

    if (!mounted) return;
    switch (result.outcome) {
      case LoginOutcome.success:
        // No navigation here: the root AuthGate is listening to AuthStore and
        // swaps this screen for HomeScreen. Replacing the root route instead
        // would tear the gate out of the tree, and with it the only thing that
        // returns the user to login when a 401 ends the session later.
        widget.onSignedIn?.call();
      case LoginOutcome.mfaRequired:
        // Password accepted; route to the second-factor code-entry screen.
        // No token is stored yet — the MFA verify mints it. MfaScreen pops
        // itself on success, so resuming here means the factor is settled.
        await Navigator.of(context).push(
          MaterialPageRoute(
            builder: (_) => MfaScreen(challenge: result.challenge!),
          ),
        );
        if (!mounted) return;
        if (AuthStore.instance.loggedIn) widget.onSignedIn?.call();
      case LoginOutcome.failure:
        // Live-region announce the failure so screen-reader users hear it
        // without re-scanning the form (WCAG 4.1.3).
        final error = AuthStore.instance.error;
        if (error != null) {
          A11y.announce(context, error);
        }
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(32),
            child: Form(
              key: _formKey,
              child: ListenableBuilder(
                listenable: AuthStore.instance,
                builder: (context, _) {
                  final auth = AuthStore.instance;
                  return Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      // Decorative: the app name is spelled out right below.
                      const Center(child: BrandMark()),
                      const SizedBox(height: 16),
                      Text(
                        l.loginAppName,
                        textAlign: TextAlign.center,
                        style: const TextStyle(
                          fontSize: 28,
                          fontWeight: FontWeight.bold,
                          letterSpacing: -0.5,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        l.loginTagline,
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          fontSize: 14,
                          // shade700 clears AA contrast (shade500 is 2.55:1).
                          color: Colors.grey.shade700,
                        ),
                      ),
                      const SizedBox(height: 32),
                      TextFormField(
                        controller: _tenantController,
                        decoration: InputDecoration(
                          labelText: l.loginTenant,
                          hintText: 'acme',
                          prefixIcon: const Icon(Icons.business),
                          border: const OutlineInputBorder(),
                        ),
                        validator: (v) =>
                            v == null || v.isEmpty ? l.loginRequired : null,
                      ),
                      const SizedBox(height: 16),
                      TextFormField(
                        controller: _emailController,
                        decoration: InputDecoration(
                          labelText: l.loginEmail,
                          prefixIcon: const Icon(Icons.email),
                          border: const OutlineInputBorder(),
                        ),
                        keyboardType: TextInputType.emailAddress,
                        autocorrect: false,
                        validator: (v) =>
                            v == null || v.isEmpty ? l.loginRequired : null,
                      ),
                      const SizedBox(height: 16),
                      TextFormField(
                        controller: _passwordController,
                        decoration: InputDecoration(
                          labelText: l.loginPassword,
                          prefixIcon: const Icon(Icons.lock),
                          border: const OutlineInputBorder(),
                          // Labelled, ≥48dp show/hide toggle (WCAG 4.1.2). The
                          // explicit Semantics label is the screen-reader name;
                          // the tooltip serves sighted hover/long-press.
                          suffixIcon: Semantics(
                            label: _obscurePassword
                                ? l.loginShowPassword
                                : l.loginHidePassword,
                            button: true,
                            child: IconButton(
                              tooltip: _obscurePassword
                                  ? l.loginShowPassword
                                  : l.loginHidePassword,
                              icon: Icon(
                                _obscurePassword
                                    ? Icons.visibility
                                    : Icons.visibility_off,
                              ),
                              onPressed: () => setState(
                                () => _obscurePassword = !_obscurePassword,
                              ),
                            ),
                          ),
                        ),
                        obscureText: _obscurePassword,
                        validator: (v) =>
                            v == null || v.isEmpty ? l.loginRequired : null,
                        onFieldSubmitted: (_) => _login(),
                      ),
                      if (auth.error != null) ...[
                        const SizedBox(height: 12),
                        Semantics(
                          liveRegion: true,
                          child: Text(
                            auth.error!,
                            // shade700 keeps the error legible at AA contrast.
                            style: TextStyle(color: Colors.red.shade700),
                            textAlign: TextAlign.center,
                          ),
                        ),
                      ],
                      const SizedBox(height: 24),
                      FilledButton(
                        onPressed: auth.loading ? null : _login,
                        style: FilledButton.styleFrom(
                          padding: const EdgeInsets.symmetric(vertical: 16),
                        ),
                        child: auth.loading
                            ? const SizedBox(
                                height: 20,
                                width: 20,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                  color: Colors.white,
                                ),
                              )
                            : Text(l.loginSignIn),
                      ),
                    ],
                  );
                },
              ),
            ),
          ),
        ),
      ),
    );
  }
}
