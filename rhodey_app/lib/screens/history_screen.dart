import 'package:flutter/material.dart';
import '../models/message.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';
import '../widgets/chat_bubble.dart';

class HistoryScreen extends StatefulWidget {
  const HistoryScreen({super.key});

  @override
  State<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends State<HistoryScreen> {
  final _api = ApiService();
  List<ChatMessage> _messages = [];
  bool _loading = true;
  String _error = '';

  @override
  void initState() {
    super.initState();
    _loadHistory();
  }

  Future<void> _loadHistory() async {
    setState(() => _loading = true);
    final result = await _api.getMessages(limit: 100);
    if (!mounted) return;

    if (result.success && result.data!.isNotEmpty) {
      _messages = result.data!.map((m) {
        final content = m['content'] as String? ?? '';
        final direction = m['direction'] as String? ?? '';
        final role = direction == 'inbound'
            ? MessageRole.user
            : MessageRole.rhodey;
        final createdAt = m['created_at'] as String? ?? '';
        final ts = createdAt.isNotEmpty
            ? (DateTime.tryParse(createdAt) ?? DateTime.now())
            : DateTime.now();
        return ChatMessage(
          id: m['id'].toString(),
          role: role,
          text: content,
          timestamp: ts,
          sendStatus: role == MessageRole.user ? SendStatus.sent : null,
        );
      }).toList();
    } else {
      _error = result.error ?? 'Failed to load history';
    }

    if (mounted) setState(() => _loading = false);
  }

  /// Group messages by date for section headers.
  Map<String, List<ChatMessage>> _groupByDate(List<ChatMessage> msgs) {
    final groups = <String, List<ChatMessage>>{};
    for (final m in msgs) {
      final key = _dateLabel(m.timestamp);
      groups.putIfAbsent(key, () => []).add(m);
    }
    return groups;
  }

  String _dateLabel(DateTime dt) {
    final now = DateTime.now();
    if (dt.year == now.year && dt.month == now.month && dt.day == now.day) {
      return 'Today';
    }
    final yesterday = now.subtract(const Duration(days: 1));
    if (dt.year == yesterday.year && dt.month == yesterday.month && dt.day == yesterday.day) {
      return 'Yesterday';
    }
    final months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return '${months[dt.month - 1]} ${dt.day}, ${dt.year}';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('History'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: AppTheme.textSecondary),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _messages.isEmpty
              ? Center(
                  child: Text(
                    _error.isNotEmpty ? '⚠️ $_error' : 'No messages yet',
                    style: AppTheme.body.copyWith(color: AppTheme.textTertiary),
                  ),
                )
              : RefreshIndicator(
                  onRefresh: _loadHistory,
                  child: ListView(
                    padding: const EdgeInsets.only(bottom: 24),
                    children: () {
                      final groups = _groupByDate(_messages.reversed.toList());
                      final entries = <Widget>[];
                      for (final date in groups.keys) {
                        entries.add(
                          Padding(
                            padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
                            child: Text(
                              date,
                              style: AppTheme.label.copyWith(
                                color: AppTheme.textTertiary,
                                fontSize: 12,
                              ),
                            ),
                          ),
                        );
                        for (final msg in groups[date]!) {
                          entries.add(ChatBubble(
                            message: msg,
                            isGroupStart: true,
                          ));
                        }
                      }
                      return entries;
                    }(),
                  ),
                ),
    );
  }
}
