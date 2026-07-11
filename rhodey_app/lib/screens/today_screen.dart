import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';
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
              _formatDate(),
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
                    ..._tasks.take(5).map((t) {
                          final taskId = t['id'] as int? ?? 0;
                          final title = t['title'] as String? ?? 'Untitled';
                          final deadline = t['deadline'] as String?;
                          final project = t['project_name'] as String?;
                          final organization = t['organization_name'] as String?;
                          final priority = t['priority'] as String?;
                          final description = t['description'] as String?;
                          return Dismissible(
                            key: ValueKey('task_$taskId'),
                            direction: DismissDirection.horizontal,
                            background: Container(
                              alignment: Alignment.centerLeft,
                              padding: const EdgeInsets.only(left: 24),
                              margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
                              decoration: BoxDecoration(
                                color: AppTheme.green.withValues(alpha: 0.2),
                                borderRadius: BorderRadius.circular(10),
                              ),
                              child: const Icon(Icons.check_circle_outline, color: AppTheme.green, size: 22),
                            ),
                            secondaryBackground: Container(
                              alignment: Alignment.centerRight,
                              padding: const EdgeInsets.only(right: 24),
                              margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
                              decoration: BoxDecoration(
                                color: AppTheme.red.withValues(alpha: 0.15),
                                borderRadius: BorderRadius.circular(10),
                              ),
                              child: const Icon(Icons.cancel_outlined, color: AppTheme.red, size: 22),
                            ),
                            confirmDismiss: (direction) async {
                              if (direction == DismissDirection.endToStart) {
                                final confirmed = await showDialog<bool>(
                                  context: context,
                                  builder: (ctx) => AlertDialog(
                                    backgroundColor: AppTheme.surface,
                                    shape: RoundedRectangleBorder(
                                      borderRadius: BorderRadius.circular(14),
                                      side: const BorderSide(color: AppTheme.border),
                                    ),
                                    title: Text('Dismiss task?',
                                      style: const TextStyle(color: AppTheme.textPrimary, fontSize: 16)),
                                    content: Text('$title will be cancelled.',
                                      style: const TextStyle(color: AppTheme.textSecondary, fontSize: 13)),
                                    actions: [
                                      TextButton(onPressed: () => Navigator.pop(ctx, false),
                                        child: const Text('Cancel', style: TextStyle(color: AppTheme.textTertiary))),
                                      TextButton(onPressed: () => Navigator.pop(ctx, true),
                                        child: const Text('Dismiss', style: TextStyle(color: AppTheme.red))),
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
                                title, taskId,
                                description: description,
                                project: project,
                                organization: organization,
                                deadline: deadline,
                                priority: priority,
                              ),
                            ),
                          );
                        }),
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

  String _formatDate() {
    final now = DateTime.now();
    final months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
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
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Center(
                child: Container(
                  width: 36, height: 4,
                  decoration: BoxDecoration(
                    color: AppTheme.border.withValues(alpha: 0.5),
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
              const SizedBox(height: 16),
              Text(title,
                style: GoogleFonts.plusJakartaSans(
                  fontSize: 18, fontWeight: FontWeight.w500,
                  color: AppTheme.textPrimary, height: 1.3,
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
                _detailRow('Organization', organization),
                const SizedBox(height: 8),
              ],
              if (deadline != null) ...[
                _detailRow('Deadline', _formatDeadline(deadline)),
                const SizedBox(height: 8),
              ],
              if (priority != null && priority.isNotEmpty && priority != 'none') ...[
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
                          child: const Text('Mark done',
                            style: TextStyle(color: AppTheme.green, fontWeight: FontWeight.w500, fontSize: 13)),
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
                          child: const Text('Dismiss',
                            style: TextStyle(color: AppTheme.red, fontWeight: FontWeight.w500, fontSize: 13)),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _detailRow(String label, String value) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 80,
          child: Text(label,
            style: GoogleFonts.plusJakartaSans(
              fontSize: 11, color: AppTheme.textTertiary, fontWeight: FontWeight.w400,
            ),
          ),
        ),
        Expanded(
          child: Text(value,
            style: GoogleFonts.plusJakartaSans(
              fontSize: 12, color: AppTheme.textSecondary, height: 1.4,
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
  final VoidCallback? onTap;
  const _TaskRow({required this.title, this.subtitle, this.isWarning = false, this.onTap});

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
        ),
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
