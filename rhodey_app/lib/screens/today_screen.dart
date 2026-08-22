import 'package:flutter/material.dart';
import '../models/today_data.dart';
import '../services/api_service.dart';
import '../services/persona.dart';
import '../services/today_cache.dart';
import '../theme/app_theme.dart';
import '../widgets/push_banner.dart';

class TodayScreen extends StatefulWidget {
  /// Push payload when this screen was opened from a notification tap. The
  /// carried `content` renders instantly (WhatsApp-style) while the real
  /// calendar/tasks load in the background.
  final Map<String, dynamic>? pushData;

  const TodayScreen({super.key, this.pushData});

  @override
  State<TodayScreen> createState() => _TodayScreenState();
}

class _TodayScreenState extends State<TodayScreen> with WidgetsBindingObserver {
  final _api = ApiService();
  final _todayCache = TodayCache();
  List<CalendarEventItem> _events = [];
  List<Map<String, dynamic>> _tasks = [];
  bool _tasksExpanded = false;
  bool _loading = true;
  String _eventError = '';

  /// Notification content carried in the push (e.g. "Team sync starts in 12
  /// min") — rendered instantly on tap so Today never shows a bare spinner
  /// before the real data arrives.
  String _pushContent = '';

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _pushContent = (widget.pushData?['content'] as String?)?.trim() ?? '';
    _paintCached();
    _loadAll();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  /// Refresh today's data when the app returns to foreground — events, tasks
  /// and captures may have changed while backgrounded.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _loadAll();
    }
  }

  /// WhatsApp-style instant render: paint the last-known snapshot from disk
  /// before any network round-trip so navigation never shows a bare spinner.
  Future<void> _paintCached() async {
    final cached = await _todayCache.load();
    if (!mounted || cached == null || cached.isEmpty) return;
    setState(() {
      _events = cached.events;
      _tasks = cached.tasks;
      _loading = false;
    });
  }

  Future<void> _loadAll() async {
    final calFut = _api.getCalendarEvents();
    // Open + committed ("I'll do it") tasks — a committed task stays visible
    // here until it's actually completed, not closed the moment it's taken on.
    final taskFut = _api.getTasks(status: 'todo,in_progress');

    // Partial render: paint each section the moment it lands instead of
    // holding a full-screen spinner until the SLOWEST call (calendar, which
    // hits the live Google/Outlook APIs) finishes.
    calFut.then((r) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        if (r.success) {
          _events = r.data!;
          _eventError = '';
        } else {
          _eventError = r.error ?? '';
        }
      });
    });
    taskFut.then((r) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        if (r.success) _tasks = r.data!;
      });
    });
    await Future.wait([calFut, taskFut]);
    if (!mounted) return;
    // Write-behind: keep the last-known snapshot for the next instant open.
    await _todayCache.save(events: _events, tasks: _tasks);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(PersonaStore.current.today),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: Text(
              _formatDate(),
              style: AppTheme.caption.copyWith(color: AppTheme.textTertiary),
            ),
          ),
        ],
      ),
      body: _loading
          ? (_pushContent.isNotEmpty
                ? ListView(
                    padding: const EdgeInsets.fromLTRB(0, 8, 0, 40),
                    children: [
                      PushBanner(title: PersonaStore.current.today, content: _pushContent),
                      const SizedBox(height: 48),
                      const Center(child: CircularProgressIndicator()),
                    ],
                  )
                : const Center(child: CircularProgressIndicator()))
          : RefreshIndicator(
              onRefresh: _loadAll,
              child: ListView(
                padding: const EdgeInsets.fromLTRB(0, 8, 0, 80),
                children: [
                  // Instant content from the notification that opened this screen
                  if (_pushContent.isNotEmpty)
                    PushBanner(
                      title: 'From your notification',
                      content: _pushContent,
                    ),

                  if (_events.isNotEmpty) ...[
                    _FocusCard(
                      item: FocusItem(
                        title: _events.first.title,
                        subtitle: _events.first.timeRange.isNotEmpty
                            ? _events.first.timeRange
                            : null,
                        action: null,
                      ),
                    ),
                    const SizedBox(height: 20),
                  ],

                  // Calendar
                  _SectionHeader(
                    title: 'Calendar',
                    trailing: _events.isNotEmpty
                        ? '+${_events.length} events ▸'
                        : null,
                  ),
                  const SizedBox(height: 8),
                  if (_eventError.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 16),
                      child: Text(
                        _eventError,
                        style: AppTheme.caption.copyWith(
                          color: AppTheme.red,
                          fontSize: 11,
                        ),
                      ),
                    )
                  else if (_events.isEmpty)
                    const Padding(
                      padding: EdgeInsets.symmetric(horizontal: 16),
                      child: Text(
                        'No events today',
                        style: TextStyle(
                          color: AppTheme.textTertiary,
                          fontSize: 13,
                        ),
                      ),
                    )
                  else
                    ..._events.map(
                      (e) => _EventRow(
                        title: e.title,
                        timeRange: e.timeRange,
                        isActive: e.isActive,
                      ),
                    ),
                  if (_events.isNotEmpty || _eventError.isNotEmpty)
                    const SizedBox(height: 20),

                  // Tasks
                  if (_tasks.isNotEmpty) ...[
                    _SectionHeader(
                      title: 'Active Tasks',
                      trailing: _tasksExpanded
                          ? 'Show less ▴'
                          : '${_tasks.length} items ▾',
                      onTap: () => setState(() => _tasksExpanded = !_tasksExpanded),
                    ),
                    const SizedBox(height: 8),
                    ...(_tasksExpanded ? _tasks : _tasks.take(5)).map((t) {
                      final taskId = t['id'] as int? ?? 0;
                      final title = t['title'] as String? ?? 'Untitled';
                      final deadline = t['deadline'] as String?;
                      final project = t['project_name'] as String?;
                      final organization = t['organization_name'] as String?;
                      final organizationId = t['organization_id'] as String?;
                      final priority = t['priority'] as String?;
                      final description = t['notes'] as String?;
                      return Dismissible(
                        key: ValueKey('task_$taskId'),
                        direction: DismissDirection.horizontal,
                        background: Container(
                          alignment: Alignment.centerLeft,
                          padding: const EdgeInsets.only(left: 24),
                          margin: const EdgeInsets.symmetric(
                            horizontal: 16,
                            vertical: 2,
                          ),
                          decoration: BoxDecoration(
                            color: AppTheme.green.withValues(alpha: 0.2),
                            borderRadius: BorderRadius.circular(10),
                          ),
                          child: const Icon(
                            Icons.check_circle_outline,
                            color: AppTheme.green,
                            size: 22,
                          ),
                        ),
                        secondaryBackground: Container(
                          alignment: Alignment.centerRight,
                          padding: const EdgeInsets.only(right: 24),
                          margin: const EdgeInsets.symmetric(
                            horizontal: 16,
                            vertical: 2,
                          ),
                          decoration: BoxDecoration(
                            color: AppTheme.red.withValues(alpha: 0.15),
                            borderRadius: BorderRadius.circular(10),
                          ),
                          child: const Icon(
                            Icons.cancel_outlined,
                            color: AppTheme.red,
                            size: 22,
                          ),
                        ),
                        confirmDismiss: (direction) async {
                          if (direction == DismissDirection.endToStart) {
                            final confirmed = await showDialog<bool>(
                              context: context,
                              builder: (ctx) => AlertDialog(
                                backgroundColor: AppTheme.surface,
                                shape: RoundedRectangleBorder(
                                  borderRadius: BorderRadius.circular(
                                    AppTheme.cardRadius,
                                  ),
                                  side: const BorderSide(
                                    color: AppTheme.border,
                                  ),
                                ),
                                title: Text(
                                  'Cancel task?',
                                  style: const TextStyle(
                                    color: AppTheme.textPrimary,
                                    fontSize: 16,
                                  ),
                                ),
                                content: Text(
                                  '$title will be cancelled.',
                                  style: const TextStyle(
                                    color: AppTheme.textSecondary,
                                    fontSize: 13,
                                  ),
                                ),
                                actions: [
                                  TextButton(
                                    onPressed: () => Navigator.pop(ctx, false),
                                    child: const Text(
                                      'Keep task',
                                      style: TextStyle(
                                        color: AppTheme.textTertiary,
                                      ),
                                    ),
                                  ),
                                  TextButton(
                                    onPressed: () => Navigator.pop(ctx, true),
                                    child: const Text(
                                      'Cancel task',
                                      style: TextStyle(color: AppTheme.red),
                                    ),
                                  ),
                                ],
                              ),
                            );
                            if (confirmed == true && mounted) {
                              await _api.updateTaskStatus(taskId, 'cancelled');
                              await _loadAll();
                            }
                            return false;
                          }
                          if (mounted) {
                            await _api.updateTaskStatus(taskId, 'done');
                            await _loadAll();
                          }
                          return false;
                        },
                        child: _TaskRow(
                          title: title,
                          subtitle: deadline != null
                              ? _formatDeadline(deadline)
                              : null,
                          isWarning: deadline != null && _isOverdue(deadline),
                          onTap: () => _showTaskDetail(
                            title,
                            taskId,
                            description: description,
                            project: project,
                            organization: organization,
                            organizationId: organizationId,
                            deadline: deadline,
                            priority: priority,
                          ),
                        ),
                      );
                    }),
                    const SizedBox(height: 20),
                  ],

                  if (_tasks.isEmpty && _events.isEmpty)
                    const Padding(
                      padding: EdgeInsets.all(32),
                      child: Center(
                        child: Text(
                          'No data yet — start by sending a message to Rhodey',
                          style: TextStyle(
                            color: AppTheme.textTertiary,
                            fontSize: 13,
                          ),
                        ),
                      ),
                    ),
                ],
              ),
            ),
    );
  }

  String _formatDate() {
    final now = DateTime.now();
    final months = [
      'Jan',
      'Feb',
      'Mar',
      'Apr',
      'May',
      'Jun',
      'Jul',
      'Aug',
      'Sep',
      'Oct',
      'Nov',
      'Dec',
    ];
    return '${months[now.month - 1]} ${now.day}';
  }

  String _formatDeadline(String dt) {
    try {
      final parsed = DateTime.parse(dt).toLocal();
      final diff = parsed.difference(DateTime.now());
      if (diff.inDays < 0) return '${diff.inDays.abs()}d overdue';
      if (diff.inDays == 0) return 'Today';
      return '${diff.inDays}d';
    } catch (_) {
      return '';
    }
  }

  bool _isOverdue(String dt) {
    try {
      return DateTime.parse(dt).toLocal().isBefore(DateTime.now());
    } catch (_) {
      return false;
    }
  }

  String _formatTimestamp(String ts) {
    try {
      final dt = DateTime.parse(ts).toLocal();
      final now = DateTime.now();
      if (dt.day == now.day && dt.month == now.month && dt.year == now.year) {
        return '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
      }
      return '${dt.month}/${dt.day}';
    } catch (_) {
      return '';
    }
  }

  void _showTaskDetail(
    String title,
    int taskId, {
    String? description,
    String? project,
    String? organization,
    String? organizationId,
    String? deadline,
    String? priority,
  }) {
    showModalBottomSheet(
      context: context,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 20),
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Center(
                child: Container(
                  width: 36,
                  height: 4,
                  decoration: BoxDecoration(
                    color: AppTheme.border.withValues(alpha: 0.5),
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
              const SizedBox(height: 16),
              Text(
                title,
                style: const TextStyle(
                  fontFamily: 'PlusJakartaSans',
                  fontSize: 18,
                  fontWeight: FontWeight.w500,
                  color: AppTheme.textPrimary,
                  height: 1.3,
                ),
              ),
              const SizedBox(height: 12),
              if (description != null && description.isNotEmpty) ...[
                _detailRow('Description', description),
                const SizedBox(height: 8),
              ],
              if (project != null) ...[
                _detailRow('Project', project),
                const SizedBox(height: 8),
              ],
              if (organization != null) ...[
                InkWell(
                  borderRadius: BorderRadius.circular(8),
                  onTap: () => _showOrgPicker(ctx, taskId, organizationId, organization),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(vertical: 4),
                    child: Row(
                      children: [
                        Expanded(child: _detailRow('Organization', organization)),
                        const Icon(Icons.chevron_right,
                            size: 16, color: AppTheme.textMuted),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 8),
              ],
              if (deadline != null) ...[
                _detailRow('Deadline', _formatDeadline(deadline)),
                const SizedBox(height: 8),
              ],
              if (priority != null &&
                  priority.isNotEmpty &&
                  priority != 'none') ...[
                _detailRow('Priority', priority.toUpperCase()),
                const SizedBox(height: 8),
              ],
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: Material(
                      color: AppTheme.green.withValues(alpha: 0.1),
                      borderRadius: BorderRadius.circular(10),
                      child: InkWell(
                        borderRadius: BorderRadius.circular(10),
                        onTap: () async {
                          Navigator.pop(ctx);
                          await _api.updateTaskStatus(taskId, 'done');
                          await _loadAll();
                        },
                        child: Container(
                          padding: const EdgeInsets.symmetric(vertical: 12),
                          alignment: Alignment.center,
                          child: const Text(
                            'Mark done',
                            style: TextStyle(
                              color: AppTheme.green,
                              fontWeight: FontWeight.w500,
                              fontSize: 13,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Material(
                      color: AppTheme.red.withValues(alpha: 0.08),
                      borderRadius: BorderRadius.circular(10),
                      child: InkWell(
                        borderRadius: BorderRadius.circular(10),
                        onTap: () async {
                          Navigator.pop(ctx);
                          await _api.updateTaskStatus(taskId, 'cancelled');
                          await _loadAll();
                        },
                        child: Container(
                          padding: const EdgeInsets.symmetric(vertical: 12),
                          alignment: Alignment.center,
                          child: const Text(
                            'Cancel task',
                            style: TextStyle(
                              color: AppTheme.red,
                              fontWeight: FontWeight.w500,
                              fontSize: 13,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  void _showOrgPicker(
    BuildContext detailCtx,
    int taskId,
    String? currentOrgId,
    String? currentOrgLabel,
  ) {
    final orgs = <Map<String, dynamic>>[];
    var loading = true;
    var query = '';

    showModalBottomSheet(
      context: context,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (pickerCtx) => StatefulBuilder(
        builder: (ctx, setState) {
          if (loading) {
            _api.getOrganizations().then((res) {
              if (res.success && mounted) {
                orgs.clear();
                orgs.addAll(List<Map<String, dynamic>>.from(res.data ?? []));
                setState(() => loading = false);
              } else if (mounted) {
                setState(() => loading = false);
              }
            });
          }
          final filtered = query.isEmpty
              ? orgs
              : orgs
                  .where((o) => (o['label'] as String? ?? '')
                      .toLowerCase()
                      .contains(query.toLowerCase()))
                  .toList();
          return SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(20, 12, 20, 20),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Center(
                    child: Container(
                      width: 36,
                      height: 4,
                      decoration: BoxDecoration(
                        color: AppTheme.border.withValues(alpha: 0.5),
                        borderRadius: BorderRadius.circular(2),
                      ),
                    ),
                  ),
                  const SizedBox(height: 16),
                  Text(
                    'Change organization',
                    style: const TextStyle(
                      fontFamily: 'PlusJakartaSans',
                      fontSize: 17,
                      fontWeight: FontWeight.w500,
                      color: AppTheme.textPrimary,
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    onChanged: (v) => setState(() => query = v),
                    decoration: InputDecoration(
                      hintText: 'Search organizations',
                      prefixIcon: const Icon(Icons.search, size: 18),
                      isDense: true,
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(10),
                        borderSide: BorderSide(color: AppTheme.border),
                      ),
                      contentPadding:
                          const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                    ),
                  ),
                  const SizedBox(height: 12),
                  if (loading)
                    const Center(
                      child: Padding(
                        padding: EdgeInsets.all(24),
                        child: CircularProgressIndicator(strokeWidth: 2),
                      ),
                    )
                  else if (filtered.isEmpty)
                    const Padding(
                      padding: EdgeInsets.all(24),
                      child: Text('No organizations found',
                          style: TextStyle(color: AppTheme.textMuted)),
                    )
                  else
                    Flexible(
                      child: ListView.separated(
                        shrinkWrap: true,
                        itemCount: filtered.length,
                        separatorBuilder: (_, _) => const Divider(height: 1),
                        itemBuilder: (_, i) {
                          final o = filtered[i];
                          final id = o['id'] as String?;
                          final selected = id == currentOrgId;
                          return ListTile(
                            dense: true,
                            title: Text(
                              o['label'] as String? ?? '',
                              style: TextStyle(
                                color: selected
                                    ? AppTheme.green
                                    : AppTheme.textPrimary,
                                fontWeight: selected
                                    ? FontWeight.w600
                                    : FontWeight.w400,
                              ),
                            ),
                            trailing: selected
                                ? const Icon(Icons.check,
                                    size: 18, color: AppTheme.green)
                                : null,
                            onTap: () async {
                              if (id == null || id == currentOrgId) {
                                Navigator.pop(ctx);
                                return;
                              }
                              Navigator.pop(ctx);
                              Navigator.pop(detailCtx);
                              final res =
                                  await _api.updateTaskOrganization(taskId, id);
                              if (res.success && mounted) {
                                ScaffoldMessenger.of(context).showSnackBar(
                                  SnackBar(
                                    content: Text(
                                        'Linked to ${o['label'] ?? 'organization'}'),
                                    duration: const Duration(seconds: 2),
                                  ),
                                );
                              } else if (mounted) {
                                ScaffoldMessenger.of(context).showSnackBar(
                                  SnackBar(
                                    content: Text(res.error ??
                                        'Failed to update organization'),
                                    backgroundColor: AppTheme.red,
                                  ),
                                );
                              }
                              await _loadAll();
                            },
                          );
                        },
                      ),
                    ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }

  Widget _detailRow(String label, String value) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 80,
          child: Text(
            label,
            style: const TextStyle(
              fontFamily: 'PlusJakartaSans',
              fontSize: 11,
              color: AppTheme.textTertiary,
              fontWeight: FontWeight.w400,
            ),
          ),
        ),
        Expanded(
          child: Text(
            value,
            style: const TextStyle(
              fontFamily: 'PlusJakartaSans',
              fontSize: 12,
              color: AppTheme.textSecondary,
              height: 1.4,
            ),
          ),
        ),
      ],
    );
  }
}

// ── Widgets ──────────────────────────────────────────────────

class _FocusCard extends StatelessWidget {
  final FocusItem item;
  const _FocusCard({required this.item});

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [AppTheme.accentBg, AppTheme.surface],
        ),
        borderRadius: BorderRadius.circular(AppTheme.cardRadius),
        border: Border.all(
          color: AppTheme.accent.withValues(alpha: 0.2),
          width: 1,
        ),
      ),
      child: Row(
        children: [
          Container(
            width: 3,
            height: 48,
            decoration: BoxDecoration(
              color: AppTheme.accent,
              borderRadius: BorderRadius.circular(2),
            ),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'FOCUS',
                  style: AppTheme.monoLabel.copyWith(color: AppTheme.accent),
                ),
                const SizedBox(height: 4),
                Text(
                  item.title,
                  style: AppTheme.displayMedium.copyWith(fontSize: 18),
                ),
                if (item.subtitle != null) ...[
                  const SizedBox(height: 2),
                  Text(
                    item.subtitle!,
                    style: AppTheme.bodySmall.copyWith(fontSize: 12),
                  ),
                ],
              ],
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            decoration: BoxDecoration(
              color: AppTheme.accent,
              borderRadius: BorderRadius.circular(8),
            ),
            child: Text(
              item.action ?? 'Focus',
              style: AppTheme.caption.copyWith(
                color: Colors.white,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _SectionHeader extends StatelessWidget {
  final String title;
  final String? trailing;
  final VoidCallback? onTap;
  const _SectionHeader({required this.title, this.trailing, this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      behavior: HitTestBehavior.opaque,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: Row(
          children: [
            Text(
              title,
              style: AppTheme.label.copyWith(
                color: AppTheme.textTertiary,
                fontSize: 11,
              ),
            ),
            const Spacer(),
            if (trailing != null)
              Text(
                trailing!,
                style: AppTheme.caption.copyWith(
                  color: AppTheme.accent,
                  fontSize: 11,
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _EventRow extends StatelessWidget {
  final String title;
  final String timeRange;
  final bool isActive;
  const _EventRow({
    required this.title,
    required this.timeRange,
    this.isActive = false,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: AppTheme.surface,
        borderRadius: BorderRadius.circular(AppTheme.cardRadius),
        border: Border.all(
          color: isActive
              ? AppTheme.accent.withValues(alpha: 0.3)
              : AppTheme.border,
          width: 1,
        ),
      ),
      child: Row(
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(
              color: isActive ? AppTheme.accent : AppTheme.textTertiary,
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              title,
              style: AppTheme.body.copyWith(
                fontSize: 13,
                color: isActive ? AppTheme.textPrimary : AppTheme.textSecondary,
              ),
            ),
          ),
          Text(timeRange, style: AppTheme.monoCaption),
        ],
      ),
    );
  }
}

class _TaskRow extends StatelessWidget {
  final String title;
  final String? subtitle;
  final bool isWarning;
  final VoidCallback? onTap;
  const _TaskRow({
    required this.title,
    this.subtitle,
    this.isWarning = false,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: onTap,
        child: Container(
          margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: isWarning ? AppTheme.redBg : AppTheme.surface,
            borderRadius: BorderRadius.circular(AppTheme.cardRadius),
            border: Border.all(
              color: isWarning
                  ? AppTheme.red.withValues(alpha: 0.2)
                  : AppTheme.border,
              width: 1,
            ),
          ),
          child: Row(
            children: [
              Text(
                isWarning ? '⚠️' : '📋',
                style: const TextStyle(fontSize: 12),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  title,
                  style: AppTheme.body.copyWith(fontSize: 13),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              if (subtitle != null)
                Text(
                  subtitle!,
                  style: AppTheme.caption.copyWith(
                    color: isWarning ? AppTheme.red : AppTheme.textTertiary,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

