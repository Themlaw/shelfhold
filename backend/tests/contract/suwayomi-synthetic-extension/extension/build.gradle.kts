// Synthetic Suwayomi extension (test only). Signed with the debug key: it is installed from this
// file in a throwaway bench, never published.
plugins {
    id("com.android.application") version "8.7.3"
    id("org.jetbrains.kotlin.android") version "2.0.21"
}

android {
    namespace = "eu.kanade.tachiyomi.extension.all.synthetic"
    compileSdk = 34
    defaultConfig {
        applicationId = "eu.kanade.tachiyomi.extension.all.synthetic"
        minSdk = 21
        targetSdk = 34
        versionCode = 1
        versionName = "1.4.1" // the extensions-lib API level the host expects
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }
    kotlinOptions { jvmTarget = "1.8" }
}

dependencies {
    // Provided by the host at run time, hence compileOnly.
    compileOnly("com.github.tachiyomiorg:extensions-lib:1.4.2")
    compileOnly("org.jetbrains.kotlin:kotlin-stdlib:2.0.21")
}
