plugins {
    id("com.android.application") version "9.5.2" apply false
    // AGP builds Kotlin itself; this pins the Kotlin version it uses, to match the Compose compiler.
    id("org.jetbrains.kotlin.android") version "2.4.21" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.4.21" apply false
}
