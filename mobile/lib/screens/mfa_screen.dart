import 'package:material_ui/material_ui.dart';
import 'package:flutter/services.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/mfa_challenge.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';

/// Second-factor code entry, shown after `AuthStore.login` returns an MFA
/// challenge. The user enters their authenticator (TOTP) code — or switches to
/// the email-OTP backup when the challenge offers it — and submits it to
/// `POST /api/auth/mfa/verify`, which mints the real JWT exactly like a clean
/// login. On success we navigate to [HomeScreen], replacing the login route.
///
/// Mirrors the web `/login/mfa` page. Passkey (WebAuthn) is intentionally
/// web-only for now and never appears as a method here.
class MfaScreen extends StatefulWidget {
  final MFAChallenge challenge;

  const MfaScreen({super.key, required this.challenge});

  @override
  State<MfaScreen> createState() => _MfaScreenState();
}

class _MfaScreenState extends State<MfaScreen> {
  final _codeController = TextEditingController();
  final _formKey = GlobalKey<FormState>();

  /// The active factor — `totp` or `email`. Defaults to TOTP when offered
  /// (the primary factor), otherwise email.
  late String _method;

  /// Whether an email OTP has been requested in this session (drives the
  /// "Resend" vs "Send code" label + the helper text).
  bool _emailRequested = false;
  bool _sendingEmail = false;

  @override
  void initState() {
    super.initState();
    _method = widget.challenge.supportsTotp ? 'totp' : 'email';
    // When email is the only factor (e.g. org-enforced MFA for an un-enrolled
    // user), auto-request the code so the "we emailed you" copy is truthful —
    // the user never had a TOTP step to act on. Deferred to after first frame
    // so `mounted` + the live region are ready for the announcement.
    if (_method == 'email') {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _requestEmailOtp();
      });
    }
  }

  @override
  void dispose() {
    _codeController.dispose();
    super.dispose();
  }

  bool get _canUseEmail => widget.challenge.supportsEmail;
  bool get _canUseTotp => widget.challenge.supportsTotp;

  Future<void> _requestEmailOtp() async {
    // Resolve the localized string before the await so we don't touch
    // BuildContext across the async gap.
    final emailedMessage = AppLocalizations.of(context).mfaEmailedAnnounce;
    setState(() => _sendingEmail = true);
    final ok = await AuthStore.instance.requestEmailOtp(
      widget.challenge.challengeToken,
    );
    if (!mounted) return;
    setState(() {
      _sendingEmail = false;
      if (ok) _emailRequested = true;
    });
    if (ok) {
      A11y.announce(context, emailedMessage);
    } else {
      final error = AuthStore.instance.error;
      if (error != null) A11y.announce(context, error);
    }
  }

  Future<void> _switchMethod(String method) async {
    if (_method == method) return;
    setState(() {
      _method = method;
      _codeController.clear();
    });
    // Auto-request the email code the first time the user switches to email.
    if (method == 'email' && !_emailRequested) {
      await _requestEmailOtp();
    }
  }

  Future<void> _verify() async {
    if (!_formKey.currentState!.validate()) return;

    final result = await AuthStore.instance.completeMfa(
      challengeToken: widget.challenge.challengeToken,
      code: _codeController.text.trim(),
      method: _method,
    );

    if (!mounted) return;
    if (result.isSuccess) {
      // Pop back to the root route, which the AuthGate now renders as the home
      // screen. Pushing HomeScreen over this route instead would bury the gate
      // and strand the user here on a later forced (401) logout.
      Navigator.of(context).pop();
    } else {
      final error = AuthStore.instance.error;
      if (error != null) A11y.announce(context, error);
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.mfaTitle)),
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
                      // Decorative shield glyph — hidden from assistive tech.
                      const ExcludeSemantics(
                        child: Icon(
                          Icons.shield_outlined,
                          size: 56,
                          color: Colors.blue,
                        ),
                      ),
                      const SizedBox(height: 16),
                      Text(
                        l.mfaHeading,
                        textAlign: TextAlign.center,
                        style: const TextStyle(
                          fontSize: 24,
                          fontWeight: FontWeight.bold,
                          letterSpacing: -0.5,
                        ),
                      ),
                      const SizedBox(height: 8),
                      Text(
                        _method == 'email' ? l.mfaPromptEmail : l.mfaPromptTotp,
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          fontSize: 14,
                          // shade700 clears AA contrast (shade500 is 2.55:1).
                          color: Colors.grey.shade700,
                        ),
                      ),
                      // Org enforces MFA but the user hasn't enrolled a factor.
                      // The mobile app can still verify by email, but setting up
                      // an authenticator app is web-only today — point them there.
                      if (widget.challenge.mustEnroll) ...[
                        const SizedBox(height: 16),
                        Container(
                          padding: const EdgeInsets.all(12),
                          decoration: BoxDecoration(
                            color: Colors.amber.shade100,
                            borderRadius: BorderRadius.circular(8),
                          ),
                          child: Text(
                            l.mfaEnforcedNotice,
                            // shade900 on the amber tint clears AA contrast.
                            style: TextStyle(color: Colors.amber.shade900),
                          ),
                        ),
                      ],
                      const SizedBox(height: 24),
                      TextFormField(
                        controller: _codeController,
                        decoration: InputDecoration(
                          labelText: l.mfaCode,
                          hintText: '123456',
                          prefixIcon: const Icon(Icons.dialpad),
                          border: const OutlineInputBorder(),
                        ),
                        keyboardType: TextInputType.number,
                        autocorrect: false,
                        autofocus: true,
                        // TOTP / email OTP are 6 digits; the backend accepts
                        // 6–8, so cap at 8 and strip non-digits.
                        maxLength: 8,
                        inputFormatters: [
                          FilteringTextInputFormatter.digitsOnly,
                        ],
                        validator: (v) {
                          final value = v?.trim() ?? '';
                          if (value.isEmpty) return l.mfaCodeRequired;
                          if (value.length < 6) return l.mfaCodeTooShort;
                          return null;
                        },
                        onFieldSubmitted: (_) => _verify(),
                      ),
                      if (auth.error != null) ...[
                        const SizedBox(height: 4),
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
                      const SizedBox(height: 20),
                      FilledButton(
                        onPressed: auth.loading ? null : _verify,
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
                            : Text(l.mfaVerify),
                      ),
                      // Email-OTP affordance — offered whenever the challenge
                      // lists `email`. Sends / resends a code to the account
                      // email, then (if TOTP isn't the active method) keeps the
                      // user on the email factor.
                      if (_canUseEmail) ...[
                        const SizedBox(height: 16),
                        if (_method == 'email')
                          TextButton.icon(
                            onPressed: _sendingEmail ? null : _requestEmailOtp,
                            icon: const Icon(Icons.mail_outline),
                            label: Text(
                              _sendingEmail
                                  ? l.mfaSending
                                  : _emailRequested
                                  ? l.mfaResendEmailCode
                                  : l.mfaSendEmailCode,
                            ),
                          )
                        else
                          TextButton(
                            onPressed: () => _switchMethod('email'),
                            child: Text(l.mfaUseEmailInstead),
                          ),
                        // Offer the way back to the authenticator code when the
                        // user switched to email but TOTP is also available.
                        if (_method == 'email' && _canUseTotp)
                          TextButton(
                            onPressed: () => _switchMethod('totp'),
                            child: Text(l.mfaUseAuthenticatorInstead),
                          ),
                      ],
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
