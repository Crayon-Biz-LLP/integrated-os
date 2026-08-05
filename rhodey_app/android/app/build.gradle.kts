import java.util.Properties

plugins {
    id("com.android.application")
    // START: FlutterFire Configuration
    id("com.google.gms.google-services")
    // END: FlutterFire Configuration
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    namespace = "com.crayon.rhodey_app"
    compileSdk = 36
    ndkVersion = flutter.ndkVersion

    compileOptions {
        isCoreLibraryDesugaringEnabled = true
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        applicationId = "com.crayon.rhodey_app"
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName

        // Resolve firebase_app_distribution_android production/staging flavor conflict.
        missingDimensionStrategy("default", "production")
    }

    signingConfigs {
        create("release") {
            // Release signing credentials come from the environment OR a local
            // (gitignored) android/key.properties — never hardcoded. The build
            // fails fast if neither is present, so a release APK can never be
            // signed with default credentials.
            val keyPropsFile = rootProject.file("key.properties")
            val keyProps = Properties().apply {
                if (keyPropsFile.exists()) {
                    keyPropsFile.inputStream().use { load(it) }
                }
            }
            fun cred(name: String, prop: String): String? =
                System.getenv(name) ?: keyProps.getProperty(prop)

            val keystorePath = cred("KEYSTORE_PATH", "storeFile")
            val storePass = cred("KEYSTORE_PASSWORD", "storePassword")
            val keyPass = cred("KEY_PASSWORD", "keyPassword")
            val keyAliasName = cred("KEY_ALIAS", "keyAlias")

            // Fail fast with a clear message — but only when a release build is
            // actually requested. This signingConfigs block is evaluated during
            // configuration of EVERY build (debug included), and debug builds
            // never sign, so they must not require credentials.
            val releaseRequested = gradle.startParameter.taskNames.any {
                it.contains("release", ignoreCase = true)
            }
            if (releaseRequested) {
                require(keystorePath != null && storePass != null &&
                    keyPass != null && keyAliasName != null) {
                    "Release signing credentials are not configured. Set the " +
                        "KEYSTORE_PATH / KEYSTORE_PASSWORD / KEY_PASSWORD / KEY_ALIAS " +
                        "environment variables or create android/key.properties with " +
                        "storeFile / storePassword / keyPassword / keyAlias."
                }
            }

            storeFile = file(keystorePath ?: "upload-keystore.jks")
            storePassword = storePass ?: ""
            keyAlias = keyAliasName ?: "upload"
            keyPassword = keyPass ?: ""
        }
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("debug")
        }
        release {
            signingConfig = signingConfigs.getByName("release")
            isMinifyEnabled = false
            isShrinkResources = false
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}

dependencies {
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.4")
}
