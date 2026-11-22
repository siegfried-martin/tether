plugins {
    id("com.android.application")
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

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release")
        }
    }
}

dependencies {
    // Trusted Web Activity launcher: opens the site full screen in Chrome.
    implementation("com.google.androidbrowserhelper:androidbrowserhelper:2.7.4")
}
