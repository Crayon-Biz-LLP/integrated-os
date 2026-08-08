import 'package:flutter/material.dart';
import 'package:flutter_web_auth_2/flutter_web_auth_2.dart';
import 'package:flutter_timezone/flutter_timezone.dart';
import '../../services/api_config.dart';
import '../../services/api_service.dart';
import '../../theme/app_theme.dart';

/// In-app onboarding journey (M8 + M9.7 + M9.8).
///
/// A short conversation that turns a new user into a seeded tenant:
///   0 Welcome → 1 key → 2 about you → 3 people → 4 plate →
///   5 life areas → 6 how Rhodey works → 7 briefing times →
///   8 Google (optional) → 9 first briefing.
///
/// Step 1 validates the API key against /api/onboarding/status; step 6
/// sets expectations (rhythm, board, approvals, memory, privacy); step 7
/// picks the briefing schedule preset (M9.7); the final step submits the
/// collected answers to /api/onboarding/complete, which seeds the tenant's
/// world (graph + tasks + settings) and returns the first welcome briefing
/// — rendered here with the same BriefingResponse shape the home screen
/// uses. [onDone] flips the app into the normal shell.
class OnboardingFlow extends StatefulWidget {
  final VoidCallback onDone;
  const OnboardingFlow({super.key, required this.onDone});

  @override
  State<OnboardingFlow> createState() => _OnboardingFlowState();
}

class _OnboardingFlowState extends State<OnboardingFlow> {
  static const _totalPages = 10;
  static const _domainSuggestions = [
    'Work', 'Business', 'Personal', 'Family', 'Health', 'Finance', 'Ministry',
  ];

  final _config = ApiConfig();
  final _api = ApiService();
  final _controller = PageController();

  int _page = 0;
  bool _busy = false;
  String? _statusText;
  bool _statusIsError = false;

  // ── Collected answers ──
  bool _keyValid = false;
  String _resolvedName = '';
  final _keyController = TextEditingController();
  final _contextController = TextEditingController();
  final _personNameController = TextEditingController();
  final _personRoleController = TextEditingController();
  final List<Map<String, String>> _people = [];
  final _taskTitleController = TextEditingController();
  String _taskPriority = 'important';
  final List<Map<String, String>> _tasks = [];
  final _domainNameController = TextEditingController();
  final _domainKeywordsController = TextEditingController();
  final List<Map<String, dynamic>> _domains = [];
  bool _googleConnected = false;
  // M9.7: briefing schedule preset (balanced = default for new tenants).
  String _presetId = 'balanced';
  // M9.7: device timezone (IANA name) sent with the seed so briefing times
  // are in HER local time from day one. Best-effort; '' lets the server keep
  // the admin-set value.
  String _deviceTimezone = '';
  Map<String, dynamic>? _briefing;
  // M9.8: briefing presets fetched from the server (single source of truth
  // for the times the gate fires on). Null until fetched — the picker then
  // renders the static offline fallback.
  Map<String, dynamic>? _presets;
  // True once the user taps a preset card — the server default must never
  // override a deliberate choice if the presets fetch lands late.
  bool _presetTouched = false;

  @override
  void initState() {
    super.initState();
    _keyController.text = _config.apiKey;
    _resolveDeviceTimezone();
    _resolvePresets();
  }

  /// Best-effort IANA timezone from the device (e.g. 'Asia/Kolkata',
  /// 'America/Toronto'). Failures leave '' — the server then keeps whatever
  /// the admin set at bootstrap.
  Future<void> _resolveDeviceTimezone() async {
    try {
      final tz = await FlutterTimezone.getLocalTimezone();
      final name = tz.identifier.trim();
      if (name.isNotEmpty && mounted) {
        setState(() => _deviceTimezone = name);
      }
    } catch (_) {
      // Non-fatal — server keeps the admin-set timezone.
    }
  }

  /// M9.8: fetch the briefing presets from the server — the picker renders
  /// THOSE times so the cards can never drift from the heartbeat gate.
  /// Fail-open: the static list in [_presetCards] is the offline fallback,
  /// so the journey never blocks on this.
  Future<void> _resolvePresets() async {
    try {
      final r = await _api.getBriefingPresets();
      if (!r.success || r.data is! Map) return;
      final presets = (r.data as Map)['presets'];
      final def = (r.data as Map)['default'];
      if (presets is! Map || presets.isEmpty || !mounted) return;
      setState(() {
        _presets = Map<String, dynamic>.from(presets);
        // Don't clobber a choice the user already made while the fetch was
        // in flight (slow network) — the default applies pre-choice only.
        if (!_presetTouched && def is String && _presets!.containsKey(def)) {
          _presetId = def;
        }
      });
    } catch (_) {
      // Non-fatal — static fallback.
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    _keyController.dispose();
    _contextController.dispose();
    _personNameController.dispose();
    _personRoleController.dispose();
    _taskTitleController.dispose();
    _domainNameController.dispose();
    _domainKeywordsController.dispose();
    super.dispose();
  }

  // ── Actions ─────────────────────────────────────────────────

  Future<void> _validateKey() async {
    setState(() {
      _busy = true;
      _statusText = null;
    });
    final key = _keyController.text.trim();
    if (key.isEmpty) {
      setState(() {
        _busy = false;
        _statusText = 'Enter the API key you received.';
        _statusIsError = true;
      });
      return;
    }
    await _config.setApiKey(key);
    final r = await _api.getOnboardingStatus();
    if (!mounted) return;
    if (r.success && r.data is Map) {
      final data = r.data as Map;
      _resolvedName = (data['name'] as String?) ?? '';
      setState(() {
        _keyValid = true;
        _busy = false;
        _statusText = _resolvedName.isNotEmpty
            ? 'Connected as $_resolvedName — welcome aboard.'
            : 'Connected — welcome aboard.';
        _statusIsError = false;
      });
    } else {
      // Don't persist a key that failed validation (M8 review fix).
      await _config.setApiKey('');
      setState(() {
        _keyValid = false;
        _busy = false;
        _statusText = 'That key was not recognized (${r.error}). Check and retry.';
        _statusIsError = true;
      });
    }
  }

  void _addPerson() {
    final name = _personNameController.text.trim();
    if (name.isEmpty) return;
    setState(() {
      _people.add({
        'name': name,
        'role': _personRoleController.text.trim(),
      });
      _personNameController.clear();
      _personRoleController.clear();
    });
  }

  void _addTask() {
    final title = _taskTitleController.text.trim();
    if (title.isEmpty) return;
    setState(() {
      _tasks.add({'title': title, 'priority': _taskPriority});
      _taskTitleController.clear();
      _taskPriority = 'important';
    });
  }

  void _addDomain({String? name, List<String>? keywords, String kind = 'work'}) {
    final n = (name ?? _domainNameController.text.trim()).trim();
    if (n.isEmpty) return;
    if (_domains.any((d) => d['name'] == n)) return;
    final kw = keywords ??
        _domainKeywordsController.text
            .split(',')
            .map((s) => s.trim())
            .where((s) => s.isNotEmpty)
            .toList();
    setState(() {
      _domains.add({'name': n, 'keywords': kw, 'kind': kind});
      _domainNameController.clear();
      _domainKeywordsController.clear();
    });
  }

  Future<void> _connectGoogle() async {
    setState(() {
      _busy = true;
      _statusText = null;
    });
    try {
      final start = await _api.startGoogleOAuth();
      if (!start.success || start.data is! Map) {
        setState(() {
          _busy = false;
          _statusText = 'Could not start Google sign-in (${start.error}).';
          _statusIsError = true;
        });
        return;
      }
      final url = (start.data as Map)['url'] as String? ?? '';
      final state = (start.data as Map)['state'] as String? ?? '';
      if (url.isEmpty) {
        setState(() {
          _busy = false;
          _statusText = 'Google sign-in is not configured on the server yet.';
          _statusIsError = true;
        });
        return;
      }
      // In-app browser → Google consent → Google redirects to the Modal
      // HTTPS callback (registered redirect URI; Google rejects custom
      // schemes) → that page JS-bridges to rhodey://oauth2/callback?code=..
      // &state=.. — flutter_web_auth_2 returns that full URL.
      final callback =
          await FlutterWebAuth2.authenticate(
            url: url,
            callbackUrlScheme: 'rhodey',
          );
      final uri = Uri.parse(callback);
      final code = uri.queryParameters['code'];
      final retState = uri.queryParameters['state'];
      if (code == null || retState != state) {
        setState(() {
          _busy = false;
          _statusText = 'Google sign-in was interrupted — please retry.';
          _statusIsError = true;
        });
        return;
      }
      final ex = await _api.exchangeGoogleOAuth(code, state);
      if (!mounted) return;
      if (ex.success) {
        setState(() {
          _googleConnected = true;
          _busy = false;
          _statusText = 'Google connected — calendar, tasks and email are live.';
          _statusIsError = false;
        });
      } else {
        setState(() {
          _busy = false;
          _statusText = 'Google connection failed (${ex.error}).';
          _statusIsError = true;
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _statusText = 'Google sign-in cancelled or failed ($e).';
        _statusIsError = true;
      });
    }
  }

  Future<void> _complete() async {
    setState(() {
      _busy = true;
      _statusText = null;
    });
    final payload = <String, dynamic>{
      'context': _contextController.text.trim(),
      'people': _people,
      'tasks': _tasks,
      'domains': _domains,
      'personal_orgs': _domains
          .where((d) => (d['kind'] as String? ?? 'work') == 'personal')
          .map((d) => d['name'] as String)
          .toList(),
      // M9.7: briefing schedule + device timezone (both optional — server
      // defaults to balanced / the admin-set timezone).
      'briefing_preset': _presetId,
      'timezone': _deviceTimezone,
    };
    final r = await _api.completeOnboarding(payload);
    if (!mounted) return;
    if (r.success && r.data is Map) {
      final data = r.data as Map;
      final briefing = data['briefing'];
      setState(() {
        _briefing = briefing is Map ? Map<String, dynamic>.from(briefing) : {};
        _busy = false;
      });
      _controller.nextPage(
        duration: AppTheme.motionBase,
        curve: AppTheme.motionCurve,
      );
    } else {
      setState(() {
        _busy = false;
        _statusText = 'Could not create your world (${r.error}). Retry.';
        _statusIsError = true;
      });
    }
  }

  void _goTo(int page) {
    if (page < 0 || page >= _totalPages) return;
    _controller.animateToPage(
      page,
      duration: AppTheme.motionBase,
      curve: AppTheme.motionCurve,
    );
  }

  void _next() {
    if (_page == _totalPages - 1) {
      widget.onDone();
      return;
    }
    _goTo(_page + 1);
  }

  // ── UI ──────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.background,
      body: SafeArea(
        child: Column(
          children: [
            if (_page > 0) _progressBar(),
            Expanded(
              child: PageView(
                controller: _controller,
                physics: const NeverScrollableScrollPhysics(),
                onPageChanged: (p) => setState(() {
                  _page = p;
                  // Status lines are per-step — never leak one step's
                  // message into the next.
                  _statusText = null;
                  _statusIsError = false;
                }),
                children: [
                  _welcomeStep(),
                  _keyStep(),
                  _aboutStep(),
                  _peopleStep(),
                  _plateStep(),
                  _areasStep(),
                  _howItWorksStep(),
                  _timesStep(),
                  _googleStep(),
                  _briefingStep(),
                ],
              ),
            ),
            _bottomBar(),
          ],
        ),
      ),
    );
  }

  Widget _progressBar() {
    return Padding(
      padding: const EdgeInsets.fromLTRB(24, 20, 24, 8),
      child: Row(
        children: List.generate(_totalPages, (i) {
          final active = i == _page;
          final done = i < _page;
          return AnimatedContainer(
            duration: AppTheme.motionBase,
            curve: AppTheme.motionCurve,
            margin: const EdgeInsets.only(right: 6),
            height: 3,
            width: active ? 26 : 12,
            decoration: BoxDecoration(
              color: done
                  ? AppTheme.accent
                  : active
                      ? AppTheme.accent.withValues(alpha: 0.6)
                      : AppTheme.border,
              borderRadius: BorderRadius.circular(2),
            ),
          );
        }),
      ),
    );
  }

  Widget _bottomBar() {
    // The welcome step has its own call-to-action — no bottom bar needed.
    if (_page == 0) return const SizedBox.shrink();
    final isLast = _page == _totalPages - 1;
    return Container(
      padding: const EdgeInsets.fromLTRB(24, 8, 24, 20),
      decoration: BoxDecoration(
        border: Border(
          top: BorderSide(color: AppTheme.border.withValues(alpha: 0.5)),
        ),
      ),
      child: Row(
        children: [
          if (_page > 0)
            TextButton(
              onPressed: _busy ? null : () => _goTo(_page - 1),
              child: Text(
                'Back',
                style: TextStyle(
                  color: AppTheme.textSecondary.withValues(alpha: 0.8),
                  fontSize: 13,
                ),
              ),
            ),
          const Spacer(),
          _primaryButton(
            label: isLast ? 'Enter Rhodey' : 'Continue',
            // The key step requires a successful validation before the
            // journey can proceed (M8 review fix).
            onTap: _busy || (_page == 1 && !_keyValid) ? null : _next,
          ),
        ],
      ),
    );
  }

  Widget _primaryButton({required String label, required VoidCallback? onTap}) {
    return Material(
      color: onTap == null ? AppTheme.textMuted : AppTheme.accent,
      borderRadius: BorderRadius.circular(AppTheme.controlRadius),
      child: InkWell(
        borderRadius: BorderRadius.circular(AppTheme.controlRadius),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 26, vertical: 12),
          child: Text(
            label,
            style: const TextStyle(
              color: Color(0xFF161510),
              fontSize: 14,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ),
    );
  }

  // ── Step 0: Welcome ─────────────────────────────────────────

  Widget _welcomeStep() {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 28),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'Rhodey',
            style: TextStyle(
              fontFamily: 'InstrumentSerif',
              fontSize: 56,
              height: 1.05,
              color: AppTheme.textPrimary,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            'Your chief of staff, in your pocket.',
            style: TextStyle(
              fontFamily: 'InstrumentSerif',
              fontSize: 21,
              fontStyle: FontStyle.italic,
              color: AppTheme.textSecondary,
            ),
          ),
          const SizedBox(height: 34),
          const _PitchRow(icon: '🧠', text: 'Knows your world — people, work, life'),
          const SizedBox(height: 14),
          const _PitchRow(icon: '🎯', text: 'Decides what matters now, not everything'),
          const SizedBox(height: 14),
          const _PitchRow(icon: '🌱', text: 'Learns from every choice you make'),
          const SizedBox(height: 44),
          _primaryButton(
            label: 'Start — it takes 3 minutes',
            onTap: () => _goTo(1),
          ),
        ],
      ),
    );
  }

  // ── Step 1: Your key ────────────────────────────────────────

  Widget _keyStep() {
    return _stepScaffold(
      title: 'Your key',
      subtitle: 'Paste the API key you were given. It connects you to Rhodey.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _fieldCard(
            controller: _keyController,
            hint: 'Paste your API key',
            obscure: true,
            onChanged: (_) => setState(() => _keyValid = false),
          ),
          const SizedBox(height: 14),
          Material(
            color: AppTheme.surfaceAlt,
            borderRadius: BorderRadius.circular(AppTheme.controlRadius),
            child: InkWell(
              borderRadius: BorderRadius.circular(AppTheme.controlRadius),
              onTap: _busy ? null : _validateKey,
              child: Container(
                padding: const EdgeInsets.symmetric(vertical: 13),
                alignment: Alignment.center,
                child: _busy
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(
                          strokeWidth: 2,
                          color: AppTheme.accent,
                        ),
                      )
                    : Text(
                        _keyValid ? '✓ Validated' : 'Validate key',
                        style: const TextStyle(
                          color: AppTheme.textPrimary,
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
              ),
            ),
          ),
          if (_statusText != null) ...[
            const SizedBox(height: 14),
            Text(
              _statusText!,
              style: TextStyle(
                color: _statusIsError ? AppTheme.red : AppTheme.green,
                fontSize: 12,
                height: 1.4,
              ),
            ),
          ],
          const Spacer(),
          if (_keyValid && _resolvedName.isNotEmpty)
            Text(
              'Welcome, $_resolvedName.',
              style: const TextStyle(
                color: AppTheme.textSecondary,
                fontSize: 13,
                fontStyle: FontStyle.italic,
              ),
            ),
        ],
      ),
    );
  }

  // ── Step 2: About you ───────────────────────────────────────

  Widget _aboutStep() {
    return _stepScaffold(
      title: 'Who are you?',
      subtitle: 'One line is enough — Rhodey learns the rest over time.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _fieldCard(
            controller: _contextController,
            hint: 'Priya, COO at Acme, Bengaluru',
          ),
          const Spacer(),
          Text(
            'Skip ahead if you prefer — you can tell Rhodey any time.',
            style: TextStyle(
              color: AppTheme.textTertiary.withValues(alpha: 0.7),
              fontSize: 11,
            ),
          ),
        ],
      ),
    );
  }

  // ── Step 3: Your people ─────────────────────────────────────

  Widget _peopleStep() {
    return _stepScaffold(
      title: 'Who\u2019s in your world?',
      subtitle: 'The people you work with and care about.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: _fieldCard(
                  controller: _personNameController,
                  hint: 'Name',
                  dense: true,
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: _fieldCard(
                  controller: _personRoleController,
                  hint: 'Role (optional)',
                  dense: true,
                ),
              ),
              const SizedBox(width: 10),
              _primaryButton(label: 'Add', onTap: _addPerson),
            ],
          ),
          const SizedBox(height: 16),
          if (_people.isEmpty)
            Text(
              'e.g. Raj — CTO, Meera — co-founder',
              style: TextStyle(
                color: AppTheme.textTertiary.withValues(alpha: 0.6),
                fontSize: 12,
              ),
            )
          else
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: _people
                  .map(
                    (p) => _chip(
                      p['name']!,
                      onRemove: () => setState(() => _people.remove(p)),
                    ),
                  )
                  .toList(),
            ),
          const Spacer(),
        ],
      ),
    );
  }

  // ── Step 4: Your plate ──────────────────────────────────────

  Widget _plateStep() {
    return _stepScaffold(
      title: 'What\u2019s on your plate?',
      subtitle: 'Two or three things you need to move right now.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: _fieldCard(
                  controller: _taskTitleController,
                  hint: 'Task',
                  dense: true,
                ),
              ),
              const SizedBox(width: 10),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8),
                decoration: BoxDecoration(
                  color: AppTheme.surface,
                  borderRadius: BorderRadius.circular(AppTheme.controlRadius),
                  border: Border.all(color: AppTheme.border),
                ),
                child: DropdownButtonHideUnderline(
                  child: DropdownButton<String>(
                    value: _taskPriority,
                    dropdownColor: AppTheme.surfaceAlt,
                    style: const TextStyle(
                      color: AppTheme.textSecondary,
                      fontSize: 12,
                    ),
                    items: const [
                      DropdownMenuItem(
                        value: 'high',
                        child: Text('High'),
                      ),
                      DropdownMenuItem(
                        value: 'important',
                        child: Text('Important'),
                      ),
                      DropdownMenuItem(
                        value: 'normal',
                        child: Text('Normal'),
                      ),
                    ],
                    onChanged: (v) => setState(() => _taskPriority = v ?? 'important'),
                  ),
                ),
              ),
              const SizedBox(width: 10),
              _primaryButton(label: 'Add', onTap: _addTask),
            ],
          ),
          const SizedBox(height: 16),
          if (_tasks.isEmpty)
            Text(
              'e.g. Prep Q3 board deck, Call Meera about hiring',
              style: TextStyle(
                color: AppTheme.textTertiary.withValues(alpha: 0.6),
                fontSize: 12,
              ),
            )
          else
            ..._tasks.map(
              (t) => Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: _taskTile(t),
              ),
            ),
          const Spacer(),
        ],
      ),
    );
  }

  // ── Step 5: Life areas ──────────────────────────────────────

  Widget _areasStep() {
    return _stepScaffold(
      title: 'Areas I should track',
      subtitle: 'Tap to add. Personal areas keep life separate from work.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final s in _domainSuggestions)
                if (!_domains.any((d) => d['name'] == s))
                  _suggestionChip(s),
              for (final d in _domains)
                _chip(
                  (d['name'] as String?) ?? '',
                  onRemove: () => setState(() => _domains.remove(d)),
                ),
            ],
          ),
          const SizedBox(height: 18),
          Row(
            children: [
              Expanded(
                child: _fieldCard(
                  controller: _domainNameController,
                  hint: 'Custom area',
                  dense: true,
                ),
              ),
              const SizedBox(width: 10),
              _primaryButton(label: 'Add', onTap: _addDomain),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            'Keywords (optional, comma-separated) — e.g. acme, client, delivery',
            style: TextStyle(
              color: AppTheme.textTertiary.withValues(alpha: 0.6),
              fontSize: 11,
            ),
          ),
          const SizedBox(height: 6),
          _fieldCard(
            controller: _domainKeywordsController,
            hint: 'keywords for the custom area',
            dense: true,
          ),
          const Spacer(),
        ],
      ),
    );
  }

  // ── Step 6: How Rhodey works (expectations) ─────────────────

  Widget _howItWorksStep() {
    return _stepScaffold(
      title: 'How Rhodey works',
      subtitle: 'Three things worth knowing before you start.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: const [
          _PitchRow(icon: '🔔', text: 'Briefings at your times — morning, midday, evening. You pick the rhythm (next step).'),
          SizedBox(height: 16),
          _PitchRow(icon: '🗂️', text: 'One living board — tasks, people, and areas. Rhodey keeps the picture current.'),
          SizedBox(height: 16),
          _PitchRow(icon: '⚖️', text: 'You stay in control — it proposes, you approve. Nothing important happens without you.'),
          SizedBox(height: 16),
          _PitchRow(icon: '🧠', text: 'It learns from you — every approve, reject, and "not now" teaches it what matters.'),
          SizedBox(height: 16),
          _PitchRow(icon: '🔐', text: 'Your data stays yours — scoped to you. Gmail is read only with your permission.'),
          Spacer(),
          Text(
            'Best to feed it daily — even one line. The more it knows your world, the sharper it gets.',
            style: TextStyle(
              color: AppTheme.textTertiary,
              fontSize: 11,
              height: 1.4,
            ),
          ),
        ],
      ),
    );
  }

  // ── Step 7: Briefing times (M9.7 preset picker) ─────────────

  Widget _timesStep() {
    return _stepScaffold(
      title: 'When should Rhodey check in?',
      subtitle: _deviceTimezone.isNotEmpty
          ? 'Times shown in your timezone ($_deviceTimezone). Your admin can adjust them anytime.'
          : 'Pick a rhythm — your admin can adjust it anytime.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Expanded(
            child: ListView(
              shrinkWrap: true,
              children: _presetCards(_presets),
            ),
          ),
        ],
      ),
    );
  }

  /// The preset cards — built from the SERVER's presets when the fetch
  /// succeeded (displayed times == gate times, single source of truth),
  /// otherwise the static offline fallback. Default preset first.
  List<Widget> _presetCards(Map<String, dynamic>? presets) {
    const defaultId = 'balanced';
    if (presets == null || presets.isEmpty) {
      return [
        _presetCard('balanced', 'Balanced',
            'Weekdays 08:00 · 13:00 · 19:00',
            'Weekends 09:00 · 17:00',
            recommended: true),
        const SizedBox(height: 10),
        _presetCard('classic', 'Classic',
            'Weekdays 07:30 · 11:30 · 14:30 · 17:30 · 20:00',
            'Weekends 08:00 · 15:00'),
        const SizedBox(height: 10),
        _presetCard('bookends', 'Bookends',
            'Weekdays 08:00 · 20:00',
            'Weekends 10:00'),
        const SizedBox(height: 10),
        _presetCard('through_the_day', 'Through the day',
            'Weekdays 08:00 · 11:00 · 14:00 · 17:00 · 20:00',
            'Weekends 08:00 · 11:00 · 17:00'),
      ];
    }
    // Default preset first, then the rest in server order (a future new
    // preset shows up automatically — nothing to copy by hand).
    final ids = presets.keys.where((id) => id != defaultId).toList();
    ids.insert(0, defaultId);
    final cards = <Widget>[];
    for (final id in ids) {
      final p = presets[id];
      if (p is! Map) continue;
      final wd = _formatTimes(p['weekday']);
      final we = _formatTimes(p['weekend']);
      cards.add(_presetCard(
        id,
        (p['name'] as String?) ?? id,
        wd.isEmpty ? 'Weekdays' : 'Weekdays $wd',
        we.isEmpty ? 'Weekends' : 'Weekends $we',
        recommended: id == defaultId,
      ));
      cards.add(const SizedBox(height: 10));
    }
    if (cards.isNotEmpty) cards.removeLast();
    return cards;
  }

  String _formatTimes(Object? raw) {
    if (raw is! List || raw.isEmpty) return '';
    return raw.map((t) => t.toString()).join(' · ');
  }

  Widget _presetCard(String id, String name, String weekday, String weekend,
      {bool recommended = false}) {
    final selected = _presetId == id;
    return Material(
      color: selected ? AppTheme.accentBg : AppTheme.surface,
      borderRadius: BorderRadius.circular(AppTheme.cardRadius),
      child: InkWell(
        borderRadius: BorderRadius.circular(AppTheme.cardRadius),
        onTap: () => setState(() {
          _presetId = id;
          _presetTouched = true;
        }),
        child: Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(AppTheme.cardRadius),
            border: Border.all(
              color: selected
                  ? AppTheme.accent
                  : AppTheme.border,
            ),
          ),
          child: Row(
            children: [
              Icon(
                selected
                    ? Icons.radio_button_checked
                    : Icons.radio_button_unchecked,
                size: 18,
                color: selected ? AppTheme.accent : AppTheme.textTertiary,
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Text(
                          name,
                          style: TextStyle(
                            color: selected
                                ? AppTheme.champagne
                                : AppTheme.textPrimary,
                            fontSize: 14,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        if (recommended) ...[
                          const SizedBox(width: 8),
                          Container(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 7,
                              vertical: 2,
                            ),
                            decoration: BoxDecoration(
                              color: AppTheme.accent.withValues(alpha: 0.2),
                              borderRadius: BorderRadius.circular(10),
                            ),
                            child: const Text(
                              'Recommended',
                              style: TextStyle(
                                color: AppTheme.champagne,
                                fontSize: 9,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                          ),
                        ],
                      ],
                    ),
                    const SizedBox(height: 4),
                    Text(
                      weekday,
                      style: TextStyle(
                        color: AppTheme.textSecondary,
                        fontSize: 11.5,
                      ),
                    ),
                    Text(
                      weekend,
                      style: TextStyle(
                        color: AppTheme.textTertiary.withValues(alpha: 0.8),
                        fontSize: 11,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  // ── Step 8: Google (optional) ───────────────────────────────

  Widget _googleStep() {
    return _stepScaffold(
      title: 'Connect Google',
      subtitle: 'Optional — but powerful. Rhodey reads your calendar, tasks, '
          'email and drive so it can act on your behalf.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const _PitchRow(icon: '📅', text: 'Calendar & Tasks'),
          const SizedBox(height: 12),
          const _PitchRow(icon: '📧', text: 'Gmail — briefings on real mail'),
          const SizedBox(height: 12),
          const _PitchRow(icon: '📁', text: 'Drive & Docs'),
          const SizedBox(height: 28),
          if (_googleConnected)
            Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                color: AppTheme.greenBg,
                borderRadius: BorderRadius.circular(AppTheme.cardRadius),
                border: Border.all(color: AppTheme.green.withValues(alpha: 0.4)),
              ),
              child: const Row(
                children: [
                  Text('✅', style: TextStyle(fontSize: 16)),
                  SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      'Google connected. Your data stays yours — Rhodey works '
                      'on your behalf, per your permission.',
                      style: TextStyle(color: AppTheme.textPrimary, fontSize: 12.5, height: 1.4),
                    ),
                  ),
                ],
              ),
            )
          else
            Material(
              color: AppTheme.surfaceAlt,
              borderRadius: BorderRadius.circular(AppTheme.controlRadius),
              child: InkWell(
                borderRadius: BorderRadius.circular(AppTheme.controlRadius),
                onTap: _busy ? null : _connectGoogle,
                child: Container(
                  padding: const EdgeInsets.symmetric(vertical: 13),
                  alignment: Alignment.center,
                  child: _busy
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: AppTheme.accent,
                          ),
                        )
                      : const Text(
                          'Connect Google',
                          style: TextStyle(
                            color: AppTheme.textPrimary,
                            fontSize: 13,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                ),
              ),
            ),
          if (_statusText != null) ...[
            const SizedBox(height: 14),
            Text(
              _statusText!,
              style: TextStyle(
                color: _statusIsError ? AppTheme.red : AppTheme.green,
                fontSize: 12,
                height: 1.4,
              ),
            ),
          ],
          const Spacer(),
          Text(
            'You can also connect later from Settings.',
            style: TextStyle(
              color: AppTheme.textTertiary.withValues(alpha: 0.6),
              fontSize: 11,
            ),
          ),
        ],
      ),
    );
  }

  // ── Step 7: First briefing ──────────────────────────────────

  Widget _briefingStep() {
    if (_briefing == null) {
      return _stepScaffold(
        title: 'Almost there',
        subtitle: 'Rhodey will now build your world from what you told us — '
            'your people, your plate, your areas.',
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const SizedBox(height: 12),
            _primaryButton(label: 'Create my world', onTap: _busy ? null : _complete),
            if (_statusText != null) ...[
              const SizedBox(height: 14),
              Text(
                _statusText!,
                style: TextStyle(
                  color: _statusIsError ? AppTheme.red : AppTheme.green,
                  fontSize: 12,
                  height: 1.4,
                ),
              ),
            ],
            const Spacer(),
          ],
        ),
      );
    }

    final b = _briefing!;
    final greeting = (b['greeting'] as String?) ?? 'Welcome.';
    final voiceLine = (b['voice_line'] as String?) ?? '';
    final sections =
        (b['sections'] as List?)?.cast<Map<String, dynamic>>() ?? const [];
    final insights =
        (b['insights'] as List?)?.cast<String>() ?? const <String>[];

    return _stepScaffold(
      title: 'Your world, as I see it',
      subtitle: null,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            greeting,
            style: const TextStyle(
              fontFamily: 'InstrumentSerif',
              fontSize: 30,
              color: AppTheme.textPrimary,
            ),
          ),
          const SizedBox(height: 8),
          if (voiceLine.isNotEmpty)
            Text(
              voiceLine,
              style: TextStyle(
                color: AppTheme.textSecondary,
                fontSize: 13.5,
                height: 1.5,
              ),
            ),
          if (insights.isNotEmpty) ...[
            const SizedBox(height: 16),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: insights
                  .map(
                    (i) => Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 10,
                        vertical: 5,
                      ),
                      decoration: BoxDecoration(
                        color: AppTheme.accentBg,
                        borderRadius: BorderRadius.circular(20),
                      ),
                      child: Text(
                        i,
                        style: const TextStyle(
                          color: AppTheme.champagne,
                          fontSize: 11,
                        ),
                      ),
                    ),
                  )
                  .toList(),
            ),
          ],
          const SizedBox(height: 20),
          Expanded(
            child: ListView(
              shrinkWrap: true,
              children: [
                for (final s in sections) ...[
                  Text(
                    (s['title'] as String?) ?? '',
                    style: TextStyle(
                      color: AppTheme.textTertiary.withValues(alpha: 0.8),
                      fontSize: 11,
                      letterSpacing: 0.6,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 8),
                  for (final item
                      in (s['items'] as List?)?.cast<Map<String, dynamic>>() ??
                          const <Map<String, dynamic>>[])
                    Container(
                      margin: const EdgeInsets.only(bottom: 6),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 9,
                      ),
                      decoration: BoxDecoration(
                        color: AppTheme.surface,
                        borderRadius: BorderRadius.circular(10),
                        border: Border.all(color: AppTheme.border),
                      ),
                      child: Row(
                        children: [
                          Text(
                            (item['icon'] as String?) ?? '•',
                            style: const TextStyle(fontSize: 14),
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Text(
                              (item['text'] as String?) ?? '',
                              style: const TextStyle(
                                color: AppTheme.textPrimary,
                                fontSize: 13,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  const SizedBox(height: 16),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  // ── Shared widgets ──────────────────────────────────────────

  Widget _stepScaffold({
    required String title,
    String? subtitle,
    required Widget child,
  }) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const SizedBox(height: 26),
          Text(
            title,
            style: const TextStyle(
              fontFamily: 'InstrumentSerif',
              fontSize: 32,
              color: AppTheme.textPrimary,
            ),
          ),
          if (subtitle != null) ...[
            const SizedBox(height: 6),
            Text(
              subtitle,
              style: TextStyle(
                color: AppTheme.textSecondary,
                fontSize: 13,
                height: 1.4,
              ),
            ),
          ],
          const SizedBox(height: 24),
          Expanded(child: child),
        ],
      ),
    );
  }

  Widget _fieldCard({
    required TextEditingController controller,
    required String hint,
    bool dense = false,
    bool obscure = false,
    ValueChanged<String>? onChanged,
  }) {
    return Container(
      decoration: BoxDecoration(
        color: AppTheme.surface,
        borderRadius: BorderRadius.circular(AppTheme.controlRadius),
        border: Border.all(color: AppTheme.border),
      ),
      child: TextField(
        controller: controller,
        obscureText: obscure,
        onChanged: onChanged,
        decoration: InputDecoration(
          hintText: hint,
          hintStyle: TextStyle(
            color: AppTheme.textTertiary.withValues(alpha: 0.6),
          ),
          border: InputBorder.none,
          contentPadding: EdgeInsets.symmetric(
            horizontal: 14,
            vertical: dense ? 12 : 14,
          ),
          isDense: dense,
        ),
        style: const TextStyle(color: AppTheme.textPrimary, fontSize: 13.5),
      ),
    );
  }

  Widget _chip(String label, {VoidCallback? onRemove}) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
      decoration: BoxDecoration(
        color: AppTheme.surfaceAlt,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: AppTheme.border),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            label,
            style: const TextStyle(color: AppTheme.textPrimary, fontSize: 12.5),
          ),
          if (onRemove != null) ...[
            const SizedBox(width: 6),
            GestureDetector(
              onTap: onRemove,
              child: const Icon(
                Icons.close,
                size: 13,
                color: AppTheme.textTertiary,
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _suggestionChip(String name) {
    return GestureDetector(
      onTap: () => _addDomain(name: name, kind: name.toLowerCase()),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
        decoration: BoxDecoration(
          color: AppTheme.accentBg,
          borderRadius: BorderRadius.circular(20),
          border: Border.all(color: AppTheme.accent.withValues(alpha: 0.25)),
        ),
        child: Text(
          '+ $name',
          style: const TextStyle(color: AppTheme.champagne, fontSize: 12.5),
        ),
      ),
    );
  }

  Widget _taskTile(Map<String, String> t) {
    final priority = t['priority'] ?? 'important';
    final color = switch (priority) {
      'high' => AppTheme.red,
      'important' => AppTheme.amber,
      _ => AppTheme.textSecondary,
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: AppTheme.surface,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppTheme.border),
      ),
      child: Row(
        children: [
          Container(
            width: 6,
            height: 6,
            decoration: BoxDecoration(color: color, shape: BoxShape.circle),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              t['title'] ?? '',
              style: const TextStyle(color: AppTheme.textPrimary, fontSize: 13),
            ),
          ),
          GestureDetector(
            onTap: () => setState(() => _tasks.remove(t)),
            child: const Icon(
              Icons.close,
              size: 14,
              color: AppTheme.textTertiary,
            ),
          ),
        ],
      ),
    );
  }
}

class _PitchRow extends StatelessWidget {
  final String icon;
  final String text;
  const _PitchRow({required this.icon, required this.text});

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Text(icon, style: const TextStyle(fontSize: 17)),
        const SizedBox(width: 12),
        Expanded(
          child: Text(
            text,
            style: const TextStyle(color: AppTheme.textSecondary, fontSize: 13.5),
          ),
        ),
      ],
    );
  }
}
