package com.marstol.tether

import android.os.Bundle
import android.util.Log
import androidx.browser.customtabs.CustomTabsCallback
import com.google.androidbrowserhelper.trusted.LauncherActivity
import com.google.androidbrowserhelper.trusted.QualityEnforcer
import com.marstol.tether.data.Sync

/**
 * The app's entry point: the library's launcher, plus a widget sync whenever the app
 * goes out of view (home, app switch), so changes made in the app show on the widgets
 * by the time the home screen is back.
 */
class TetherLauncherActivity : LauncherActivity() {
    override fun getCustomTabsCallback(): CustomTabsCallback {
        val app = applicationContext
        return object : QualityEnforcer() {
            override fun onNavigationEvent(navigationEvent: Int, extras: Bundle?) {
                super.onNavigationEvent(navigationEvent, extras)
                if (navigationEvent == TAB_HIDDEN) {
                    Log.d("TetherSync", "app hidden: syncing widgets")
                    Sync.now(app)
                }
            }
        }
    }
}
