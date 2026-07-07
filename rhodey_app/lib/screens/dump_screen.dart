import 'package:flutter/material.dart';
import '../models/capture_item.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';
import '../widgets/voice_states.dart';

class DumpScreen extends StatefulWidget {
  const DumpScreen({super.key});

  @override
  State<DumpScreen> createState() => _DumpScreenState();
}

class _DumpScreenState extends State<DumpScreen> {
  final _textController = TextEditingController();
  final _api = ApiService();
  VoiceState _voiceState = VoiceState.idle;
  String? _transcribedText;
  bool _loading = true;
  List<CaptureItem> _captures = [];

  @override
  void initState() {
    super.initState();
    _loadCaptures();
  }

  /// Fetch real captures from /api/captures.
  Future<void> _loadCaptures() async {
    final result = await _api.getCaptures(limit: 30);
    if (!mounted) return;
    if (result.success && result.data!.isNotEmpty) {
      _captures = result.data!.map((c) {
        final content = c['content'] as String? ?? '';
        final createdAt = c['created_at'] as String? ?? '';
        final ts = createdAt.isNotEmpty
            ? (DateTime.tryParse(createdAt) ?? DateTime.now())
            : DateTime.now();
        final source = c['source'] as String? ?? '';
        final msgType = c['message_type'] as String? ?? c['status'] as String? ?? 'done';
        final status = msgType == 'pending' || msgType == 'processing'
            ? CaptureStatus.processing
            : CaptureStatus.done;
        return CaptureItem(
          id: c['id'].toString(),
          type: source == 'voice' ? CaptureType.voice :
                 source == 'photo' ? CaptureType.photo : CaptureType.text,
          status: status,
          summary: content.length > 100 ? '${content.substring(0, 100)}...' : content,
          timestamp: ts,
          resultLabel: status == CaptureStatus.done ? 'Stored' : null,
        );
      }).toList();
    }
    if (mounted) setState(() => _loading = false);
  }

  void _toggleVoice() {
    if (_voiceState == VoiceState.idle) {
      setState(() => _voiceState = VoiceState.listening);
      Future.delayed(const Duration(seconds: 2), () {
        if (!mounted) return;
        setState(() {
          _voiceState = VoiceState.transcribing;
          _transcribedText = 'Remind me to call Sunju about school tomorrow';
        });
        Future.delayed(const Duration(seconds: 1), () {
          if (!mounted) return;
          setState(() => _voiceState = VoiceState.confirm);
        });
      });
    } else {
      setState(() {
        _voiceState = VoiceState.idle;
        _transcribedText = null;
      });
    }
  }

  /// Sends a raw capture to Rhodey via /api/send-message.
  /// Shows optimistic processing state, then resolves to done on success.
  Future<void> _capture(String text) async {
    if (text.trim().isEmpty) return;
    final id = DateTime.now().millisecondsSinceEpoch.toString();
    setState(() {
      _captures.insert(0, CaptureItem(
        id: id,
        type: CaptureType.text,
        status: CaptureStatus.processing,
        summary: text.trim(),
        timestamp: DateTime.now(),
      ));
    });
    _textController.clear();

    // Send to Rhodey backend
    final result = await _api.sendMessage(text.trim());
    if (!mounted) return;
    setState(() {
      _captures[0] = _captures[0].copyWith(
        status: result.success ? CaptureStatus.done : CaptureStatus.processing,
        resultLabel: result.success ? 'Sent to Rhodey' : null,
      );
    });
  }

  @override
  void dispose() {
    _textController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final todayCaptures = _captures.where((c) => c.sameDayAs).toList();
    final yesterdayCaptures = _captures.where((c) => !c.sameDayAs).toList();

    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            const Text('Captures'),
            const Spacer(),
            if (_api.config.isConfigured && !_loading)
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  color: AppTheme.greenBg,
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Text(
                  '${_captures.length} items',
                  style: AppTheme.statusDot.copyWith(
                    color: AppTheme.green,
                  ),
                ),
              ),
          ],
        ),
      ),
      body: Column(
        children: [
          // Quick capture area
          Container(
            margin: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: AppTheme.surface,
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: AppTheme.border),
            ),
            child: Column(
              children: [
                // Big capture button
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 20, 20, 12),
                  child: GestureDetector(
                    onTap: _toggleVoice,
                    child: Container(
                      width: double.infinity,
                      padding: const EdgeInsets.symmetric(vertical: 24),
                      decoration: BoxDecoration(
                        color: AppTheme.accentBg,
                        borderRadius: BorderRadius.circular(12),
                        border: Border.all(
                          color: AppTheme.accent.withValues(alpha: 0.2),
                        ),
                      ),
                      child: Column(
                        children: [
                          Icon(
                            _voiceState == VoiceState.idle
                                ? Icons.mic
                                : Icons.stop,
                            color: AppTheme.accent,
                            size: 32,
                          ),
                          const SizedBox(height: 8),
                          Text(
                            _voiceState == VoiceState.idle
                                ? 'Tap to start recording'
                                : 'Recording... tap to stop',
                            style: AppTheme.bodySmall.copyWith(
                              color: AppTheme.accent,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),

                // Text input
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                  child: Row(
                    children: [
                      Expanded(
                        child: Container(
                          decoration: BoxDecoration(
                            color: AppTheme.surfaceAlt,
                            borderRadius: BorderRadius.circular(10),
                            border: Border.all(color: AppTheme.border),
                          ),
                          child: TextField(
                            controller: _textController,
                            textInputAction: TextInputAction.send,
                            onSubmitted: _capture,
                            decoration: const InputDecoration(
                              hintText: 'Or type a raw thought...',
                              border: InputBorder.none,
                              contentPadding: EdgeInsets.symmetric(
                                horizontal: 14,
                                vertical: 10,
                              ),
                              isDense: true,
                            ),
                            style: AppTheme.body,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      IconButton(
                        icon: const Icon(Icons.image_outlined,
                            color: AppTheme.textTertiary, size: 22),
                        onPressed: () {},
                      ),
                      IconButton(
                        icon: const Icon(Icons.description_outlined,
                            color: AppTheme.textTertiary, size: 22),
                        onPressed: () {},
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),

          // Voice state
          if (_voiceState != VoiceState.idle)
            VoiceStateMachine(
              state: _voiceState,
              transcribedText: _transcribedText,
              onCancel: _toggleVoice,
              onTaskConfirm: () {
                if (_transcribedText != null) _capture(_transcribedText!);
                setState(() {
                  _voiceState = VoiceState.done;
                });
                Future.delayed(const Duration(seconds: 2), () {
                  if (!mounted) return;
                  setState(() {
                    _voiceState = VoiceState.idle;
                    _transcribedText = null;
                  });
                });
              },
              onNoteConfirm: () {
                if (_transcribedText != null) _capture(_transcribedText!);
                setState(() {
                  _voiceState = VoiceState.done;
                });
                Future.delayed(const Duration(seconds: 2), () {
                  if (!mounted) return;
                  setState(() {
                    _voiceState = VoiceState.idle;
                    _transcribedText = null;
                  });
                });
              },
              onRetry: () => _toggleVoice(),
            ),

          // Timeline
          Expanded(
            child: ListView(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              children: [
                if (todayCaptures.isNotEmpty)
                  _SectionHeader(
                    title: "Today's Captures",
                    count: todayCaptures.length,
                  ),
                ...todayCaptures.map((c) => _CaptureRow(item: c)),

                if (yesterdayCaptures.isNotEmpty) ...[
                  const SizedBox(height: 16),
                  _SectionHeader(title: 'Yesterday', count: yesterdayCaptures.length),
                  ...yesterdayCaptures.map((c) => _CaptureRow(item: c)),
                ],
                const SizedBox(height: 80),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _SectionHeader extends StatelessWidget {
  final String title;
  final int count;

  const _SectionHeader({required this.title, required this.count});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Text(
        '$title · $count items',
        style: AppTheme.label.copyWith(
          color: AppTheme.textTertiary,
          fontSize: 11,
        ),
      ),
    );
  }
}

class _CaptureRow extends StatelessWidget {
  final CaptureItem item;

  const _CaptureRow({required this.item});

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 4),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: AppTheme.surface,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppTheme.border, width: 1),
      ),
      child: Row(
        children: [
          Text(item.typeIcon, style: const TextStyle(fontSize: 14)),
          const SizedBox(width: 10),
          Text(
            item.statusIndicator,
            style: const TextStyle(fontSize: 10),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              item.summary,
              style: AppTheme.body.copyWith(fontSize: 13),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
          if (item.resultLabel != null)
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
              decoration: BoxDecoration(
                color: AppTheme.accentBg,
                borderRadius: BorderRadius.circular(6),
              ),
              child: Text(
                item.resultLabel!,
                style: AppTheme.caption.copyWith(
                  color: AppTheme.accent,
                  fontSize: 10,
                ),
              ),
            ),
          const SizedBox(width: 6),
          Text(
            _formatTime(item.timestamp),
            style: AppTheme.caption.copyWith(fontSize: 10),
          ),
        ],
      ),
    );
  }

  String _formatTime(DateTime dt) {
    final now = DateTime.now();
    if (dt.day == now.day && dt.month == now.month && dt.year == now.year) {
      return '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
    }
    return '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
  }
}
