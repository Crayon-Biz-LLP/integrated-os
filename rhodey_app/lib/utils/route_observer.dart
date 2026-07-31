import 'package:flutter/widgets.dart';

/// Global route observer so screens can refresh when they regain focus.
///
/// Registered on the MaterialApp's `navigatorObservers`. Screens mix in
/// `RouteAware` and override `didPopNext()` to reload live data (e.g. the
/// home screen refreshes its Inbox pending count when the Inbox pops — no
/// matter which path pushed it).
final RouteObserver<ModalRoute<void>> routeObserver =
    RouteObserver<ModalRoute<void>>();
