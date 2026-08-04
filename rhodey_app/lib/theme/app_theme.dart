import 'package:flutter/material.dart';
/// Rhodey Surface Theme — Editorial Premium
///
/// Design principles:
///   - Warm stone/charcoal base (not pure black, not cold)
///   - Champagne accent for premium warmth
///   - Typography-first: serif greeting, clean sans body
///   - One material language, no glassmorphism base
///   - Restrained spacing, crisp borders
///
/// Fonts are bundled in APK assets/fonts/ — zero network overhead.
/// Font families: InstrumentSerif, PlusJakartaSans, JetBrainsMono.
class AppTheme {
  AppTheme._();

  // ── Warm Stone Palette ──
  static const Color background = Color(0xFF0C0C0B);
  static const Color surface = Color(0xFF161618);
  static const Color surfaceAlt = Color(0xFF1E1E1D);
  static const Color border = Color(0xFF2C2C30);
  static const Color borderLight = Color(0xFF333337);

  static const Color textPrimary = Color(0xFFEDE9E4);
  static const Color textSecondary = Color(0xFF7A756E);
  static const Color textTertiary = Color(0xFF6B6863);
  static const Color textMuted = Color(0xFF4A4743);

  // Accents
  static const Color champagne = Color(0xFFDFCCA7);
  static const Color champagneDark = Color(0xFFA69275);
  static const Color champagneMuted = Color(0x26DFCCA7); // 15% opacity

  /// Success / completion green — a muted sage that holds up on the warm
  /// charcoal base. (Was mistakenly aliased to champagne, which made every
  /// acknowledgement card render the same gold as the accent.)
  static const Color green = Color(0xFF8FBF8C);
  static const Color greenBg = Color(0x1A8FBF8C);
  static const Color amber = Color(0xFFFFD60A);
  static const Color amberBg = Color(0x1AFFD60A);
  static const Color red = Color(0xFFEF5350);
  static const Color redBg = Color(0x1AEF5350);
  static const Color blue = Color(0xFF5E9EFF);

  // ── Typography (Google Fonts — not compile-time const) ──

  /// Editorial serif greeting (InstrumentSerif, large, italic, light)
  /// Fonts bundled in APK — zero network overhead.
  static TextStyle get greetingStyle => const TextStyle(
        fontFamily: 'InstrumentSerif',
        fontSize: 30,
        fontWeight: FontWeight.w300,
        fontStyle: FontStyle.italic,
        color: textPrimary,
        height: 1.2,
      );

  /// Sub-greeting / paragraph (smaller, muted, regular)
  static TextStyle get subGreetingStyle => const TextStyle(
        fontFamily: 'PlusJakartaSans',
        fontSize: 13,
        fontWeight: FontWeight.w300,
        color: textSecondary,
        height: 1.5,
      );

  /// Section title (9px mono, uppercase, wide tracking)
  static TextStyle get sectionTitleStyle => const TextStyle(
        fontFamily: 'JetBrainsMono',
        fontSize: 9,
        fontWeight: FontWeight.w400,
        color: textTertiary,
        letterSpacing: 2.0,
        height: 1.3,
      );

  /// Item body text (13px, light, clean)
  static TextStyle get bodyStyle => const TextStyle(
        fontFamily: 'PlusJakartaSans',
        fontSize: 13,
        fontWeight: FontWeight.w300,
        color: textPrimary,
        height: 1.4,
      );

  /// Muted body (secondary info)
  static TextStyle get bodyMuted => const TextStyle(
        fontFamily: 'PlusJakartaSans',
        fontSize: 13,
        fontWeight: FontWeight.w300,
        color: textSecondary,
        height: 1.4,
      );

  /// Action chip label (10px, medium)
  static TextStyle get chipStyle => const TextStyle(
        fontFamily: 'PlusJakartaSans',
        fontSize: 10,
        fontWeight: FontWeight.w500,
        color: textPrimary,
        height: 1.2,
      );

  /// Dock label (10px)
  static TextStyle get dockStyle => const TextStyle(
        fontFamily: 'PlusJakartaSans',
        fontSize: 10,
        fontWeight: FontWeight.w400,
        color: textSecondary,
        height: 1.2,
      );

  /// Segmented control label (9px mono, uppercase)
  static TextStyle get segmentStyle => const TextStyle(
        fontFamily: 'JetBrainsMono',
        fontSize: 9,
        fontWeight: FontWeight.w500,
        letterSpacing: 1.5,
        height: 1.2,
      );

  /// Proactive card headline (12px)
  static TextStyle get proactiveStyle => const TextStyle(
        fontFamily: 'PlusJakartaSans',
        fontSize: 12,
        fontWeight: FontWeight.w400,
        color: textPrimary,
        height: 1.4,
      );

  /// Presence label (12px, medium)
  static TextStyle get presenceStyle => const TextStyle(
        fontFamily: 'PlusJakartaSans',
        fontSize: 12,
        fontWeight: FontWeight.w500,
        color: textSecondary,
        height: 1.2,
      );

  /// Empty state hint (11px, italic)
  static TextStyle get hintStyle => const TextStyle(
        fontFamily: 'PlusJakartaSans',
        fontSize: 11,
        fontStyle: FontStyle.italic,
        fontWeight: FontWeight.w300,
        color: textMuted,
        height: 1.4,
      );

  // ── Card grammar (one consistent language across all screens) ──

  /// Standard card corner radius — every card/banner/row uses this so the
  /// surface grammar is uniform app-wide (bubbles keep their chat shape).
  static const double cardRadius = 12;

  /// Small control radius (chips, action buttons, badges).
  static const double controlRadius = 8;

  // ── Warm motion (Chief-of-Staff calm — fade + gentle rise, never bounce) ──

  static const Duration motionBase = Duration(milliseconds: 350);

  /// Ease curve for entrances — soft deceleration, no overshoot.
  static const Curve motionCurve = Curves.easeOutCubic;

  // ── System/notation typography (JetBrainsMono) ──
  //
  // Rhodey's voice is serif (InstrumentSerif); conversational body is
  // PlusJakartaSans. JetBrainsMono is reserved for NON-Rhodey system
  // notation: timestamps, counts, confidence, type labels — the "ledger"
  // text that reads like a clock or a terminal, not a person.

  /// Timestamps, counts, time ranges (10px mono).
  static const TextStyle monoCaption = TextStyle(
    fontFamily: 'JetBrainsMono',
    fontSize: 10,
    fontWeight: FontWeight.w400,
    color: textTertiary,
    height: 1.3,
    letterSpacing: 0.2,
  );

  /// Type/label badges — CLARIFICATION, NEW PERSON, N PENDING (9px mono caps).
  static const TextStyle monoLabel = TextStyle(
    fontFamily: 'JetBrainsMono',
    fontSize: 9,
    fontWeight: FontWeight.w500,
    color: textTertiary,
    height: 1.2,
    letterSpacing: 1.2,
  );

  // ── Legacy getters (compile-time const, for backward compat with legacy screens) ──

  /// Legacy accent color
  static const Color accent = champagne;

  /// Legacy accent background
  static const Color accentBg = champagneMuted;

  /// Legacy body text — now explicitly PlusJakartaSans so the chat surface
  /// (bubbles, card bodies) actually uses the design font instead of falling
  /// back to the platform default (Roboto on Android).
  static const TextStyle body = TextStyle(
    fontFamily: 'PlusJakartaSans',
    fontSize: 14,
    fontWeight: FontWeight.w400,
    color: textPrimary,
    height: 1.5,
  );

  /// Legacy small body
  static const TextStyle bodySmall = TextStyle(
    fontFamily: 'PlusJakartaSans',
    fontSize: 13,
    fontWeight: FontWeight.w400,
    color: textSecondary,
    height: 1.4,
  );

  /// Legacy caption
  static const TextStyle caption = TextStyle(
    fontFamily: 'PlusJakartaSans',
    fontSize: 11,
    fontWeight: FontWeight.w500,
    color: textTertiary,
    height: 1.3,
    letterSpacing: 0.2,
  );

  /// Legacy label
  static const TextStyle label = TextStyle(
    fontFamily: 'PlusJakartaSans',
    fontSize: 12,
    fontWeight: FontWeight.w600,
    color: textSecondary,
    height: 1.3,
    letterSpacing: 0.5,
  );

  /// Legacy title
  static const TextStyle title = TextStyle(
    fontFamily: 'PlusJakartaSans',
    fontSize: 15,
    fontWeight: FontWeight.w600,
    color: textPrimary,
    height: 1.3,
  );

  /// Legacy status dot
  static const TextStyle statusDot = TextStyle(
    fontFamily: 'PlusJakartaSans',
    fontSize: 10,
    fontWeight: FontWeight.w600,
    height: 1.0,
  );

  /// Legacy display medium
  static const TextStyle displayMedium = TextStyle(
    fontFamily: 'PlusJakartaSans',
    fontSize: 22,
    fontWeight: FontWeight.w600,
    color: textPrimary,
    height: 1.3,
    letterSpacing: -0.3,
  );

  /// Legacy chat bubble colors
  ///
  /// userBubble was a cold navy (0xFF1E2A3A) — the one color that broke the
  /// warm stone/champagne language. Now a warm charcoal, slightly lighter than
  /// the bot's bubble so the two still read as distinct (borders + alignment
  /// differentiate them further).
  static const Color userBubble = Color(0xFF23211E);
  static const Color botBubble = Color(0xFF1A1A1E);

  // ── Theme Data ──
  static ThemeData get themeData {
    final textTheme = TextTheme(
      displayLarge: greetingStyle,
      displayMedium: subGreetingStyle,
      titleMedium: sectionTitleStyle,
      bodyLarge: bodyStyle,
      bodyMedium: bodyMuted,
      labelSmall: chipStyle,
    );

    return ThemeData.dark().copyWith(
      brightness: Brightness.dark,
      scaffoldBackgroundColor: background,
      colorScheme: const ColorScheme.dark(
        primary: champagne,
        secondary: champagneDark,
        surface: surface,
        onPrimary: Color(0xFF0C0C0B),
        onSecondary: Color(0xFF0C0C0B),
        onSurface: textPrimary,
      ),
      textTheme: textTheme,
      appBarTheme: AppBarTheme(
        backgroundColor: background,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleTextStyle: const TextStyle(
          fontFamily: 'PlusJakartaSans',
          fontSize: 15,
          fontWeight: FontWeight.w500,
          color: textPrimary,
        ),
      ),
      dividerTheme: const DividerThemeData(
        color: border,
        thickness: 1,
        space: 0,
      ),
      cardTheme: CardThemeData(
        color: surface,
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(10),
          side: const BorderSide(color: border, width: 1),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: surfaceAlt,
        contentPadding:
            const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: border),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: border),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: champagne),
        ),
        hintStyle: const TextStyle(
          fontFamily: 'PlusJakartaSans',
          fontSize: 13,
          fontWeight: FontWeight.w300,
          color: textTertiary,
        ),
      ),
    );
  }
}
