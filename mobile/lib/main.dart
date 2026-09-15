import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/home_screen.dart';
import 'package:feohledger_mobile/screens/login_screen.dart';
import 'package:feohledger_mobile/services/biometric_service.dart';
import 'package:feohledger_mobile/services/push_service.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/locale_store.dart';
import 'package:feohledger_mobile/widgets/brand_mark.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // Load the device's persisted display-language choice before first build so
  // MaterialApp.locale is correct on the very first frame.
  await LocaleStore.instance.init();
  runApp(const APApp());
}

class APApp extends StatelessWidget {
  const APApp({super.key});

  @override
  Widget build(BuildContext context) {
    // Rebuild MaterialApp whenever the device locale choice changes so the
    // whole tree re-localizes live (no restart). `locale: null` follows the
    // system locale (resolved against supportedLocales).
    return ListenableBuilder(
      listenable: LocaleStore.instance,
      builder: (context, _) {
        return MaterialApp(
          title: 'FeohLedger',
          debugShowCheckedModeBanner: false,
          navigatorKey: PushService.navigatorKey,
          locale: LocaleStore.instance.locale,
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          theme: ThemeData(
            colorScheme: ColorScheme.fromSeed(seedColor: Colors.blue),
            useMaterial3: true,
            appBarTheme: const AppBarTheme(
              centerTitle: false,
              elevation: 0,
            ),
          ),
          home: const AuthGate(),
        );
      },
    );
  }
}

/// The app's root route: **a function of auth state**, not a one-shot
/// navigation decision.
///
/// A forced logout can be raised from anywhere — `ApiClient` tears the session
/// down on a 401 from any verb, and `AuthStore.reset()` notifies. When the
/// only route to the home screen was an imperative `pushReplacement` fired once
/// at startup, nothing above `HomeScreen` was listening: a 401 left the user
/// sitting on a home screen with no user, every tab erroring and no way back to
/// login except quitting the app. Rendering the root from
/// `AuthStore.instance.loggedIn` makes "401 responses auto-clear session **and
/// return to login**" true by construction.
class AuthGate extends StatefulWidget {
  const AuthGate({super.key});

  @override
  State<AuthGate> createState() => _AuthGateState();
}

class _AuthGateState extends State<AuthGate> {
  /// True until the one-time startup work (push init, session restore,
  /// biometric unlock) has finished; the splash shows meanwhile.
  bool _booting = true;

  /// A restored session that failed the device biometric check. The credentials
  /// are deliberately kept (a failed Face ID says nothing about the token, and
  /// clearing would wipe the offline cache), but the app stays on the login
  /// screen until the user authenticates again.
  bool _biometricLocked = false;

  /// Last observed sign-in state, so a `loggedIn -> signed out` transition can
  /// be told apart from the notifier's other emissions (loading flags).
  bool _wasLoggedIn = false;

  @override
  void initState() {
    super.initState();
    AuthStore.instance.addListener(_onAuthChanged);
    _boot();
  }

  @override
  void dispose() {
    AuthStore.instance.removeListener(_onAuthChanged);
    super.dispose();
  }

  Future<void> _boot() async {
    // Initialize push notifications (no-op if Firebase not configured)
    await PushService.instance.init();

    final hasSession = await AuthStore.instance.init();
    var locked = false;
    if (hasSession && await BiometricService.instance.isEnabled) {
      locked = !await BiometricService.instance.authenticate();
    }
    if (!mounted) return;
    setState(() {
      _booting = false;
      _biometricLocked = locked;
    });
  }

  void _onAuthChanged() {
    final loggedIn = AuthStore.instance.loggedIn;
    final signedOut = _wasLoggedIn && !loggedIn;
    _wasLoggedIn = loggedIn;
    if (!signedOut) return;
    // The gate itself is the FIRST route, so a session that ends while the user
    // is inside a pushed route (invoice detail, admin, a modal) would leave the
    // login screen rendered underneath and invisible. Drop everything above it.
    // Deferred to the end of the frame: this runs inside notifyListeners(),
    // which can fire mid-build.
    SchedulerBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      Navigator.maybeOf(context)?.popUntil((route) => route.isFirst);
    });
  }

  /// A successful sign-in through the gate's own login screen clears the
  /// biometric lock — the user has just proven who they are with credentials.
  void _onSignedIn() {
    if (!_biometricLocked) return;
    setState(() => _biometricLocked = false);
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: AuthStore.instance,
      builder: (context, _) {
        if (_booting) return const SplashScreen();
        if (AuthStore.instance.loggedIn && !_biometricLocked) {
          return const HomeScreen();
        }
        return LoginScreen(onSignedIn: _onSignedIn);
      },
    );
  }
}

/// Startup placeholder. Purely visual — the boot sequence and the routing
/// decision both live in [AuthGate].
class SplashScreen extends StatelessWidget {
  const SplashScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            BrandMark(),
            SizedBox(height: 16),
            CircularProgressIndicator(),
          ],
        ),
      ),
    );
  }
}
