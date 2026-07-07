enum CaptureStatus { processing, done, failed }

enum CaptureType { text, voice, photo, document }

class CaptureItem {
  final String id;
  final CaptureType type;
  final CaptureStatus status;
  final String summary;
  final String? fullText;
  final DateTime timestamp;
  final String? resultLabel;

  const CaptureItem({
    required this.id,
    required this.type,
    required this.status,
    required this.summary,
    this.fullText,
    required this.timestamp,
    this.resultLabel,
  });

  CaptureItem copyWith({
    CaptureType? type,
    CaptureStatus? status,
    String? summary,
    String? fullText,
    DateTime? timestamp,
    String? resultLabel,
  }) {
    return CaptureItem(
      id: id,
      type: type ?? this.type,
      status: status ?? this.status,
      summary: summary ?? this.summary,
      fullText: fullText ?? this.fullText,
      timestamp: timestamp ?? this.timestamp,
      resultLabel: resultLabel ?? this.resultLabel,
    );
  }

  String get typeIcon {
    switch (type) {
      case CaptureType.text:
        return '✏️';
      case CaptureType.voice:
        return '🎤';
      case CaptureType.photo:
        return '📷';
      case CaptureType.document:
        return '📄';
    }
  }

  String get statusIndicator {
    switch (status) {
      case CaptureStatus.done:
        return '🟢';
      case CaptureStatus.processing:
        return '🟡';
      case CaptureStatus.failed:
        return '🔴';
    }
  }

  bool get sameDayAs => timestamp.day == DateTime.now().day;
}
