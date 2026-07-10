import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

/// Rhodey Surface Theme — Editorial Premium
///
/// Design principles:
///   - Warm stone/charcoal base (not pure black, not cold)
///   - Champagne accent for premium warmth
///   - Typography-first: serif greeting, clean sans body
///   - One material language, no glassmorphism base
///   - Restrained spacing, crisp borders
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

  static const Color green = Color(0xFF34C759);
  static const Color greenBg = Color(0x1A34C759);
  static const Color amber = Color(0xFFFFD60A);
  static const Color amberBg = Color(0x1AFFD60A);
  static const Color red = Color(0xFFEF5350);
  static const Color redBg = Color(0x1AEF5350);
  static const Color blue = Color(0xFF5E9EFF);

  // ── Typography (Google Fonts — not compile-time const) ──

  /// Editorial serif greeting (Instrument Serif, large, italic, light)
  static TextStyle get greetingStyle => GoogleFonts.instrumentSerif(
        fontSize: 30,
        fontWeight: FontWeight.w300,
        fontStyle: FontStyle.italic,
        color: textPrimary,
        height: 1.2,
      );

  /// Sub-greeting / paragraph (smaller, muted, regular)
  static TextStyle get subGreetingStyle => GoogleFonts.plusJakartaSans(
        fontSize: 13,
        fontWeight: FontWeight.w300,
        color: textSecondary,
        height: 1.5,
      );

  /// Section title (9px mono, uppercase, wide tracking)
  static TextStyle get sectionTitleStyle => GoogleFonts.jetBrainsMono(
        fontSize: 9,
        fontWeight: FontWeight.w400,
        color: textTertiary,
        letterSpacing: 2.0,
        height: 1.3,
      );

  /// Item body text (13px, light, clean)
  static TextStyle get bodyStyle => GoogleFonts.plusJakartaSans(
        fontSize: 13,
        fontWeight: FontWeight.w300,
        color: textPrimary,
        height: 1.4,
      );

  /// Muted body (secondary info)
  static TextStyle get bodyMuted => GoogleFonts.plusJakartaSans(
        fontSize: 13,
        fontWeight: FontWeight.w300,
        color: textSecondary,
        height: 1.4,
      );

  /// Action chip label (10px, medium)
  static TextStyle get chipStyle => GoogleFonts.plusJakartaSans(
        fontSize: 10,
        fontWeight: FontWeight.w500,
        color: textPrimary,
        height: 1.2,
      );

  /// Dock label (10px)
  static TextStyle get dockStyle => GoogleFonts.plusJakartaSans(
        fontSize: 10,
        fontWeight: FontWeight.w400,
        color: textSecondary,
        height: 1.2,
      );

  /// Segmented control label (9px mono, uppercase)
  static TextStyle get segmentStyle => GoogleFonts.jetBrainsMono(
        fontSize: 9,
        fontWeight: FontWeight.w500,
        letterSpacing: 1.5,
        height: 1.2,
      );

  /// Proactive card headline (12px)
  static TextStyle get proactiveStyle => GoogleFonts.plusJakartaSans(
        fontSize: 12,
        fontWeight: FontWeight.w400,
        color: textPrimary,
        height: 1.4,
      );

  /// Presence label (12px, medium)
  static TextStyle get presenceStyle => GoogleFonts.plusJakartaSans(
        fontSize: 12,
        fontWeight: FontWeight.w500,
        color: textSecondary,
        height: 1.2,
      );

  /// Empty state hint (11px, italic)
  static TextStyle get hintStyle => GoogleFonts.plusJakartaSans(
        fontSize: 11,
        fontStyle: FontStyle.italic,
        fontWeight: FontWeight.w300,
        color: textMuted,
        height: 1.4,
      );

  // ── Legacy getters (compile-time const, for backward compat with legacy screens) ──

  /// Legacy accent color
  static const Color accent = champagne;

  /// Legacy accent background
  static const Color accentBg = champagneMuted;

  /// Legacy body text
  static const TextStyle body = TextStyle(
    fontSize: 14,
    fontWeight: FontWeight.w400,
    color: textPrimary,
    height: 1.5,
  );

  /// Legacy small body
  static const TextStyle bodySmall = TextStyle(
    fontSize: 13,
    fontWeight: FontWeight.w400,
    color: textSecondary,
    height: 1.4,
  );

  /// Legacy caption
  static const TextStyle caption = TextStyle(
    fontSize: 11,
    fontWeight: FontWeight.w500,
    color: textTertiary,
    height: 1.3,
    letterSpacing: 0.2,
  );

  /// Legacy label
  static const TextStyle label = TextStyle(
    fontSize: 12,
    fontWeight: FontWeight.w600,
    color: textSecondary,
    height: 1.3,
    letterSpacing: 0.5,
  );

  /// Legacy title
  static const TextStyle title = TextStyle(
    fontSize: 15,
    fontWeight: FontWeight.w600,
    color: textPrimary,
    height: 1.3,
  );

  /// Legacy status dot
  static const TextStyle statusDot = TextStyle(
    fontSize: 10,
    fontWeight: FontWeight.w600,
    height: 1.0,
  );

  /// Legacy display medium
  static const TextStyle displayMedium = TextStyle(
    fontSize: 22,
    fontWeight: FontWeight.w600,
    color: textPrimary,
    height: 1.3,
    letterSpacing: -0.3,
  );

  /// Legacy chat bubble colors
  static const Color userBubble = Color(0xFF1E2A3A);
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
        titleTextStyle: GoogleFonts.plusJakartaSans(
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
        hintStyle: GoogleFonts.plusJakartaSans(
          fontSize: 13,
          fontWeight: FontWeight.w300,
          color: textTertiary,
        ),
      ),
    );
  }
}
