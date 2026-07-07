import 'package:flutter/material.dart';
import '../models/today_data.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';

class TodayScreen extends StatefulWidget {
  const TodayScreen({super.key});

  @override
  State<TodayScreen> createState() => _TodayScreenState();
}

class _TodayScreenState extends State<TodayScreen> {
  final _api = ApiService();
  List<CalendarEventItem> _events = [];
  List<Map<String, dynamic>> _tasks = [];
  List<Map<String, dynamic>> _captures = [];
  bool _loading = true;
  String _eventError = '';

  static final _focus = FocusItem(
    title: 'Equisoft Sync Call',
    subtitle: '10:00 AM — Deck ready in Drive',
    action: 'Prepare',
  );

  @override
  void initState() {
    super.initState();
    _loadAll();
  }

  Future<void> _loadAll() async {
    final calFut = _api.getCalendarEvents();
    final taskFut = _api.getTasks();
    final capFut = _api.getCaptures(limit: 10);

    final calResult = await calFut;
    final taskResult = await taskFut;
    final capResult = await capFut;

    if (!mounted) return;
    setState(() {
      _loading = false;
      if (calResult.success) {
        _events = calResult.data!;
      } else {
        _eventError = calResult.error ?? '';
      }
      if (taskResult.success) {
        _tasks = taskResult.data!;
      }
      if (capResult.success) {
        _captures = capResult.data!;
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Today'),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: Text(
              'Jul 7',
              style: AppTheme.caption.copyWith(color: AppTheme.textTertiary),
            ),
          ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : RefreshIndicator(
              onRefresh: _loadAll,
              child: ListView(
                padding: const EdgeInsets.fromLTRB(0, 8, 0, 80),
                children: [
                  _FocusCard(item: _focus),
                  const SizedBox(height: 20),

                  // Calendar
                  _SectionHeader(
                    title: 'Calendar',
                    trailing: _events.isNotEmpty ? '+${_events.length} events ▸' : null,
                  ),
                  const SizedBox(height: 8),
                  if (_eventError.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 16),
                      child: Text(_eventError,
                          style: AppTheme.caption.copyWith(color: AppTheme.red, fontSize: 11)),
                    )
                  else if (_events.isEmpty)
                    const Padding(
                      padding: EdgeInsets.symmetric(horizontal: 16),
                      child: Text('No events today',
                          style: TextStyle(color: AppTheme.textTertiary, fontSize: 13)),
                    )
                  else
                    ..._events.map((e) => _EventRow(
                          title: e.title,
                          timeRange: e.timeRange,
                          isActive: e.isActive,
                        )),
                  if (_events.isNotEmpty || _eventError.isNotEmpty)
                    const SizedBox(height: 20),

                  // Tasks
                  if (_tasks.isNotEmpty) ...[
                    _SectionHeader(
                      title: 'Active Tasks',
                      trailing: '${_tasks.length} items ▸',
                    ),
                    const SizedBox(height: 8),
                    ..._tasks.take(5).map((t) => _TaskRow(
                          title: t['title'] as String? ?? 'Untitled',
                          subtitle: t['deadline'] != null
                              ? _formatDeadline(t['deadline'] as String)
                              : null,
                          isWarning: t['deadline'] != null &&
                              _isOverdue(t['deadline'] as String),
                        )),
                    const SizedBox(height: 20),
                  ],

                  // Captures
                  if (_captures.isNotEmpty) ...[
                    _SectionHeader(
                      title: "Recent Captures",
                      trailing: '${_captures.length} items ▸',
                    ),
                    const SizedBox(height: 8),
                    ..._captures.take(5).map((c) => _CaptureRowPreview(
                          title: c['content'] as String? ?? '',
                          time: _formatTimestamp(c['created_at'] as String? ?? ''),
                        )),
                    const SizedBox(height: 20),
                  ],

                  if (_tasks.isEmpty && _captures.isEmpty && _events.isEmpty)
                    const Padding(
                      padding: EdgeInsets.all(32),
                      child: Center(
                        child: Text('No data yet — start by sending a message to Rhodey',
                            style: TextStyle(color: AppTheme.textTertiary, fontSize: 13)),
                      ),
                    ),
                ],
              ),
            ),
    );
  }

  String _formatDeadline(String dt) {
    try {
      final parsed = DateTime.parse(dt);
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
      return DateTime.parse(dt).isBefore(DateTime.now());
    } catch (_) {
      return false;
    }
  }

  String _formatTimestamp(String ts) {
    try {
      final dt = DateTime.parse(ts);
      final now = DateTime.now();
      if (dt.day == now.day && dt.month == now.month && dt.year == now.year) {
        return '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
      }
      return '${dt.month}/${dt.day}';
    } catch (_) {
      return '';
    }
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
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: AppTheme.accent.withValues(alpha: 0.2), width: 1,
        ),
      ),
      child: Row(
        children: [
          Container(
            width: 3, height: 48,
            decoration: BoxDecoration(
              color: AppTheme.accent, borderRadius: BorderRadius.circular(2),
            ),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('FOCUS', style: AppTheme.statusDot.copyWith(
                  color: AppTheme.accent, fontSize: 9, letterSpacing: 1.5,
                )),
                const SizedBox(height: 4),
                Text(item.title, style: AppTheme.displayMedium.copyWith(fontSize: 18)),
                if (item.subtitle != null) ...[
                  const SizedBox(height: 2),
                  Text(item.subtitle!, style: AppTheme.bodySmall.copyWith(fontSize: 12)),
                ],
              ],
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            decoration: BoxDecoration(
              color: AppTheme.accent, borderRadius: BorderRadius.circular(8),
            ),
            child: Text(
              item.action ?? 'Focus',
              style: AppTheme.caption.copyWith(
                color: Colors.white, fontWeight: FontWeight.w600,
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
  const _SectionHeader({required this.title, this.trailing});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16),
      child: Row(
        children: [
          Text(title, style: AppTheme.label.copyWith(
            color: AppTheme.textTertiary, fontSize: 11,
          )),
          const Spacer(),
          if (trailing != null)
            Text(trailing!, style: AppTheme.caption.copyWith(
              color: AppTheme.accent, fontSize: 11,
            )),
        ],
      ),
    );
  }
}

class _EventRow extends StatelessWidget {
  final String title;
  final String timeRange;
  final bool isActive;
  const _EventRow({
    required this.title, required this.timeRange, this.isActive = false,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: AppTheme.surface,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(
          color: isActive ? AppTheme.accent.withValues(alpha: 0.3) : AppTheme.border,
          width: 1,
        ),
      ),
      child: Row(
        children: [
          Container(
            width: 8, height: 8,
            decoration: BoxDecoration(
              color: isActive ? AppTheme.accent : AppTheme.textTertiary,
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(title, style: AppTheme.body.copyWith(
              fontSize: 13,
              color: isActive ? AppTheme.textPrimary : AppTheme.textSecondary,
            )),
          ),
          Text(timeRange, style: AppTheme.caption.copyWith(fontSize: 11)),
        ],
      ),
    );
  }
}

class _TaskRow extends StatelessWidget {
  final String title;
  final String? subtitle;
  final bool isWarning;
  const _TaskRow({required this.title, this.subtitle, this.isWarning = false});

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: isWarning ? AppTheme.redBg : AppTheme.surface,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(
          color: isWarning ? AppTheme.red.withValues(alpha: 0.2) : AppTheme.border,
          width: 1,
        ),
      ),
      child: Row(
        children: [
          Text(isWarning ? '⚠️' : '📋', style: const TextStyle(fontSize: 12)),
          const SizedBox(width: 10),
          Expanded(
            child: Text(title,
              style: AppTheme.body.copyWith(fontSize: 13),
              maxLines: 1, overflow: TextOverflow.ellipsis,
            ),
          ),
          if (subtitle != null)
            Text(subtitle!, style: AppTheme.caption.copyWith(
              color: isWarning ? AppTheme.red : AppTheme.textTertiary,
              fontSize: 12, fontWeight: FontWeight.w600,
            )),
        ],
      ),
    );
  }
}

class _CaptureRowPreview extends StatelessWidget {
  final String title;
  final String time;
  const _CaptureRowPreview({required this.title, required this.time});

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: AppTheme.surface, borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppTheme.border, width: 1),
      ),
      child: Row(
        children: [
          const Text('🟢', style: TextStyle(fontSize: 10)),
          const SizedBox(width: 10),
          Expanded(child: Text(title, style: AppTheme.body.copyWith(fontSize: 13),
              maxLines: 1, overflow: TextOverflow.ellipsis)),
          Text(time, style: AppTheme.caption.copyWith(fontSize: 10)),
        ],
      ),
    );
  }
}
