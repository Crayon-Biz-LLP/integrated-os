allprojects {
    repositories {
        google()
        mavenCentral()
    }
}

subprojects {
    // Force compileSdk for all subprojects (including plugins like firebase_app_distribution_android)
    // to prevent "compiled against android-30" errors from library modules.
    // Uses explicit extension type checks instead of the generated `android { }` accessor,
    // which is only available in build scripts where the Android plugin is directly applied.
    afterEvaluate {
        val androidExtension = extensions.findByName("android")
        if (androidExtension != null) {
            when (androidExtension) {
                is com.android.build.gradle.LibraryExtension -> androidExtension.compileSdk = 36
                is com.android.build.api.dsl.ApplicationExtension -> androidExtension.compileSdk = 36
            }
        }
    }
}

val newBuildDir: Directory =
    rootProject.layout.buildDirectory
        .dir("../../build")
        .get()
rootProject.layout.buildDirectory.value(newBuildDir)

subprojects {
    val newSubprojectBuildDir: Directory = newBuildDir.dir(project.name)
    project.layout.buildDirectory.value(newSubprojectBuildDir)
}
subprojects {
    project.evaluationDependsOn(":app")
}

tasks.register<Delete>("clean") {
    delete(rootProject.layout.buildDirectory)
}
