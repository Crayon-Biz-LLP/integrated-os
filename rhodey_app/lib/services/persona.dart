import 'package:shared_preferences/shared_preferences.dart';

/// M15: the onboarding role persona — a vocabulary layer over the SAME app.
///
/// The engine, briefings, focal-card decisions and approvals are shared for
/// now (one voice, disclosed on the picker). The persona only renames the
/// app's surfaces and onboarding copy, so a household user never stares at
/// "Entities" or "chief of staff". Chosen on the welcome screen, persisted
/// server-side (user_settings.persona via onboarding/complete) and mirrored
/// here in SharedPreferences so the app shell labels resolve instantly.
///
/// M15.1: the two workhorse surfaces are UNIVERSAL by decision — everyone
/// sees "Quick Confirmations" (the things to confirm) and "People & Orgs"
/// (the people/orgs Rhodey knows). Persona variety lives on the focal +
/// pulse names, area presets, and the About-You example instead.
class PersonaLabels {
  final String id;
  final String name; // short display name on the picker card
  final String pitch; // one-liner on the picker card
  final String today;
  final String inbox;
  final String entities;
  final String history;
  final String focal; // the focal-card concept
  final String pulse; // the time-of-day check-in concept
  final String aboutHint; // 'Who are you?' example
  final List<String> areaPresets;

  const PersonaLabels({
    required this.id,
    required this.name,
    required this.pitch,
    required this.today,
    required this.inbox,
    required this.entities,
    required this.history,
    required this.focal,
    required this.pulse,
    required this.aboutHint,
    required this.areaPresets,
  });
}

const Map<String, PersonaLabels> _personas = {
  'chief_staff': PersonaLabels(
    id: 'chief_staff',
    name: 'Chief of Staff',
    pitch: 'For founders and executives balancing strategy, team alignment, '
        'and the decisions that matter.',
    today: 'Today',
    inbox: 'Quick Confirmations',
    entities: 'People & Orgs',
    history: 'History',
    focal: 'Top priority',
    pulse: 'Executive check-in',
    aboutHint: 'COO at Acme, leading a 20-person team, Bengaluru',
    areaPresets: ['Work', 'Business', 'Strategy', 'Team', 'Personal'],
  ),
  'ops_copilot': PersonaLabels(
    id: 'ops_copilot',
    name: 'Operations Co-Pilot',
    pitch: 'For freelancers, agency leads, and PMs juggling client deadlines, '
        'projects, and deliverables.',
    today: 'Active Desk',
    inbox: 'Quick Confirmations',
    entities: 'People & Orgs',
    history: 'History',
    focal: 'Next deliverable',
    pulse: 'Workstream pulse',
    aboutHint: 'Freelance designer running three client projects solo',
    areaPresets: ['Clients', 'Projects', 'Delivery', 'Personal'],
  ),
  'organizer': PersonaLabels(
    id: 'organizer',
    name: 'Daily Organizer',
    pitch: 'For busy professionals who want to filter out daily noise and '
        'get on with what\u2019s open.',
    today: 'Daily Focus',
    inbox: 'Quick Confirmations',
    entities: 'People & Orgs',
    history: 'History',
    focal: 'Focus now',
    pulse: 'Daily rhythm',
    aboutHint: 'Marketing manager — planning, meetings, and deadlines',
    areaPresets: ['Work', 'Personal', 'Errands'],
  ),
  'household': PersonaLabels(
    id: 'household',
    name: 'Life & Household',
    pitch: 'For managing family logistics, personal goals, household admin, '
        'and the things you don\u2019t want to forget.',
    today: 'Today',
    inbox: 'Quick Confirmations',
    entities: 'People & Orgs',
    history: 'History',
    focal: 'Don\u2019t forget',
    pulse: 'Morning-evening check',
    aboutHint: 'I manage our home, the kids\u2019 schedules, and our family finances',
    areaPresets: ['Family', 'Home', 'Health', 'Kids', 'School'],
  ),
  'simple': PersonaLabels(
    id: 'simple',
    name: 'Keep it simple',
    pitch: 'Plain words — no titles, no jargon. Rhodey just works.',
    today: 'Today',
    inbox: 'Quick Confirmations',
    entities: 'People & Orgs',
    history: 'History',
    focal: 'What matters now',
    pulse: 'Daily check-in',
    aboutHint: 'What you do, and what matters in a typical week',
    areaPresets: ['Work', 'Personal', 'Family', 'Health'],
  ),
};

/// The shared disclaimer on the persona picker — honest that the current
/// version speaks one voice for everyone (briefings unchanged for now).
const String kPersonaDisclaimer =
    'Every style runs on the same engine — for now, briefings and check-ins '
    'arrive in Rhodey\u2019s standard voice. Rhodey adopts your style fully in '
    'an upcoming update.';

class PersonaStore {
  static const String _key = 'rhodey_persona';
  static String _currentId = 'chief_staff';

  static PersonaLabels get current => _personas[_currentId] ?? _personas['chief_staff']!;
  static String get currentId => _currentId;
  static List<String> get ids => _personas.keys.toList(growable: false);
  static PersonaLabels of(String id) => _personas[id] ?? _personas['chief_staff']!;

  /// Load the persisted persona (defaults to chief_staff — today's voice).
  static Future<void> load() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final id = prefs.getString(_key) ?? 'chief_staff';
      _currentId = _personas.containsKey(id) ? id : 'chief_staff';
    } catch (_) {
      // Non-fatal — keep the default.
    }
  }

  /// Persist a persona choice (app-shell labels apply immediately).
  static Future<void> set(String id) async {
    if (!_personas.containsKey(id)) return;
    _currentId = id;
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_key, id);
    } catch (_) {
      // Non-fatal — in-memory still applies for this session.
    }
  }
}
