class FocusItem {
  final String title;
  final String? subtitle;
  final String? action;
  final bool isUpcoming;

  const FocusItem({
    required this.title,
    this.subtitle,
    this.action,
    this.isUpcoming = true,
  });
}

class CalendarEvent {
  final String id;
  final String title;
  final String timeRange;
  final bool isActive;

  const CalendarEvent({
    required this.id,
    required this.title,
    required this.timeRange,
    this.isActive = false,
  });
}

class OverdueItem {
  final String id;
  final String title;
  final int daysOverdue;

  const OverdueItem({
    required this.id,
    required this.title,
    required this.daysOverdue,
  });
}

class SectionLink {
  final String label;
  final int count;
  final String targetTab;

  const SectionLink({
    required this.label,
    required this.count,
    required this.targetTab,
  });
}
