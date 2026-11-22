plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Release signing comes from ~/.gradle/gradle.properties, never from the repo (see README.md).
val keystore = providers.gradleProperty("TETHER_KEYSTORE").orNull

android {
    namespace = "com.marstol.tether"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.marstol.tether"
        minSdk = 31
        targetSdk = 37
        versionCode = 1
        versionName = "1.0"
    }

    signingConfigs {
        if (keystore != null) {
            create("release") {
                storeFile = file(keystore)
                storePassword = providers.gradleProperty("TETHER_KEYSTORE_PASSWORD").get()
                keyAlias = providers.gradleProperty("TETHER_KEY_ALIAS").get()
                keyPassword = providers.gradleProperty("TETHER_KEY_PASSWORD").get()
            }
        }
    }

    buildFeatures {
        compose = true   // Glance widgets are written in Compose
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release")
        }
    }
}

dependencies {
    // Trusted Web Activity launcher: opens the site full screen in Chrome.
    implementation("com.google.androidbrowserhelper:androidbrowserhelper:2.7.4")
    // Home-screen widgets, their background refresh, and the sign-in tab.
    implementation("androidx.glance:glance-appwidget:1.2.0")
    implementation("androidx.glance:glance-material3:1.2.0")
    implementation("androidx.work:work-runtime-ktx:2.12.0")
    implementation("androidx.browser:browser:1.10.0")
}
