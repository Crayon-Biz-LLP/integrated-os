import 'package:flutter/material.dart';
import '../theme/app_theme.dart';

/// Voice interaction states.
///
/// REAL ANDROID FLOW:
///   idle → listening → transcribing → understanding → confirm → done → idle
///                                                      ↘ done (auto if high conf)
///   idle → listening → transcribing → error → idle
///
/// Permission model (Android):
///   RECORD_AUDIO — must be granted before entering [listening].
///   On first tap, request permission. If denied, show rationale, then
///   skip to [error] with "Microphone permission required."
///
/// Foreground service:
///   While in [listening] or [transcribing], a foreground notification
///   is required (Android 14+). The notification shows "Rhodey is listening"
///   and allows tap-to-stop.
///
/// Transitions:
///   [listening] → [transcribing]: triggered by silence timeout (1.5s) or
///     manual stop. On-device SpeechRecognizer processes the audio.
///   [transcribing] → [understanding]: transcription result ready.
///     If confidence > threshold (0.85), skip [confirm] → [done] directly.
///   [understanding] → [confirm]: confidence below threshold, ask user.
///   [confirm] → [done]: user picks Task or Note, text sent to backend.
///   [done] → [idle]: auto-dismiss after 3s, or manual dismiss.
///   any → [error]: permission denied, speech timeout, network failure.
///   [error] → [idle]: user taps dismiss.
enum VoiceState {
  idle,
  listening,
  transcribing,
  understanding,
  confirm,
  done,
  error,
}

class VoiceStateMachine extends StatelessWidget {
  final VoiceState state;
  final String? transcribedText;
  final String? errorMessage;
  final VoidCallback? onCancel;
  final VoidCallback? onTaskConfirm;
  final VoidCallback? onNoteConfirm;
  final VoidCallback? onRetry;

  const VoiceStateMachine({
    super.key,
    required this.state,
    this.transcribedText,
    this.errorMessage,
    this.onCancel,
    this.onTaskConfirm,
    this.onNoteConfirm,
    this.onRetry,
  });

  @override
  Widget build(BuildContext context) {
    if (state == VoiceState.idle) return const SizedBox.shrink();

    return Container(
      margin: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: AppTheme.surface,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: AppTheme.border, width: 1),
      ),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            _buildStateIndicator(),
            const SizedBox(height: 16),
            if (transcribedText != null && transcribedText!.isNotEmpty)
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(14),
                decoration: BoxDecoration(
                  color: AppTheme.surfaceAlt,
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text(
                  transcribedText!,
                  style: AppTheme.body.copyWith(
                    color: AppTheme.textPrimary,
                    height: 1.5,
                  ),
                ),
              ),
            if (errorMessage != null && state == VoiceState.error) ...[
              const SizedBox(height: 8),
              Text(
                errorMessage!,
                style: AppTheme.bodySmall.copyWith(color: AppTheme.red),
                textAlign: TextAlign.center,
              ),
            ],
            const SizedBox(height: 16),
            _buildActions(),
          ],
        ),
      ),
    );
  }

  Widget _buildStateIndicator() {
    switch (state) {
      case VoiceState.listening:
        return Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const _PulsingDot(),
            const SizedBox(width: 10),
            Text(
              'Listening...',
              style: AppTheme.title.copyWith(color: AppTheme.accent),
            ),
          ],
        );
      case VoiceState.transcribing:
        return Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const SizedBox(
              width: 16,
              height: 16,
              child: CircularProgressIndicator(
                strokeWidth: 2,
                color: AppTheme.amber,
              ),
            ),
            const SizedBox(width: 10),
            Text(
              'Transcribing...',
              style: AppTheme.title.copyWith(color: AppTheme.amber),
            ),
          ],
        );
      case VoiceState.understanding:
        return Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const SizedBox(
              width: 16,
              height: 16,
              child: CircularProgressIndicator(
                strokeWidth: 2,
                color: AppTheme.accent,
              ),
            ),
            const SizedBox(width: 10),
            Text(
              'Rhodey is thinking...',
              style: AppTheme.title.copyWith(color: AppTheme.accent),
            ),
          ],
        );
      case VoiceState.confirm:
        return Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Text('🤔', style: TextStyle(fontSize: 18)),
            const SizedBox(width: 10),
            Text(
              'Is this a task or a note?',
              style: AppTheme.title,
            ),
          ],
        );
      case VoiceState.done:
        return Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.check_circle, color: AppTheme.green, size: 18),
            const SizedBox(width: 10),
            Text(
              'Captured',
              style: AppTheme.title.copyWith(color: AppTheme.green),
            ),
          ],
        );
      case VoiceState.error:
        return Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.error_outline, color: AppTheme.red, size: 18),
            const SizedBox(width: 10),
            Text(
              'Something went wrong',
              style: AppTheme.title.copyWith(color: AppTheme.red),
            ),
          ],
        );
      default:
        return const SizedBox.shrink();
    }
  }

  Widget _buildActions() {
    switch (state) {
      case VoiceState.listening:
        return Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            _ActionChip(
              label: 'Stop',
              icon: Icons.stop,
              color: AppTheme.red,
              onTap: onCancel,
            ),
          ],
        );
      case VoiceState.transcribing:
      case VoiceState.understanding:
        return const SizedBox.shrink();
      case VoiceState.confirm:
        return Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            _ActionChip(
              label: 'Task',
              icon: Icons.checklist,
              color: AppTheme.accent,
              onTap: onTaskConfirm,
            ),
            const SizedBox(width: 8),
            _ActionChip(
              label: 'Note',
              icon: Icons.note_outlined,
              color: AppTheme.amber,
              onTap: onNoteConfirm,
            ),
            const SizedBox(width: 8),
            _ActionChip(
              label: 'Cancel',
              icon: Icons.close,
              color: AppTheme.textTertiary,
              onTap: onCancel,
            ),
          ],
        );
      case VoiceState.done:
        return Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            _ActionChip(
              label: 'Dismiss',
              color: AppTheme.textTertiary,
              onTap: onCancel,
            ),
          ],
        );
      case VoiceState.error:
        return Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            _ActionChip(
              label: 'Retry',
              icon: Icons.refresh,
              color: AppTheme.accent,
              onTap: onRetry,
            ),
            const SizedBox(width: 8),
            _ActionChip(
              label: 'Dismiss',
              color: AppTheme.textTertiary,
              onTap: onCancel,
            ),
          ],
        );
      default:
        return const SizedBox.shrink();
    }
  }
}

class _PulsingDot extends StatefulWidget {
  const _PulsingDot();

  @override
  State<_PulsingDot> createState() => _PulsingDotState();
}

class _PulsingDotState extends State<_PulsingDot>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;
  late Animation<double> _animation;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 800),
    )..repeat(reverse: true);
    _animation = Tween<double>(begin: 0.4, end: 1.0).animate(
      CurvedAnimation(parent: _controller, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _animation,
      builder: (context, child) {
        return Container(
          width: 14,
          height: 14,
          decoration: BoxDecoration(
            color: AppTheme.accent.withValues(alpha: _animation.value),
            shape: BoxShape.circle,
          ),
        );
      },
    );
  }
}

class _ActionChip extends StatelessWidget {
  final String label;
  final IconData? icon;
  final Color color;
  final VoidCallback? onTap;

  const _ActionChip({
    required this.label,
    this.icon,
    required this.color,
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
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: color.withValues(alpha: 0.4), width: 1),
            color: color.withValues(alpha: 0.08),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (icon != null) ...[
                Icon(icon, size: 16, color: color),
                const SizedBox(width: 6),
              ],
              Text(
                label,
                style: AppTheme.title.copyWith(
                  color: color,
                  fontSize: 13,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
