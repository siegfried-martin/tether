package com.marstol.tether.widget

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import kotlinx.coroutines.flow.MutableStateFlow
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.glance.Button
import androidx.glance.ColorFilter
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.LocalContext
import androidx.glance.action.Action
import androidx.glance.action.ActionParameters
import androidx.glance.action.actionStartActivity
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionStartActivity
import androidx.glance.appwidget.appWidgetBackground
import androidx.glance.appwidget.cornerRadius
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.ColumnScope
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.size
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextAlign
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import androidx.glance.color.ColorProvider as DayNightColorProvider
import com.marstol.tether.R
import com.marstol.tether.SignInActivity
import com.marstol.tether.data.Api
import com.marstol.tether.data.Store
import com.marstol.tether.data.Sync
import com.marstol.tether.data.Tokens

/** Per-widget state: the Tasks widget's tag filter, the Inbox widget's account, and the Day widget's day (0 = today). */
val TAG_ID = intPreferencesKey("tag_id")
val ACCOUNT = stringPreferencesKey("account")
val DAY_OFFSET = intPreferencesKey("day_offset")
/** Bumped on every sync: widgets read their data from files, so this is what makes them redraw. */
val DATA_VERSION = intPreferencesKey("data_version")

/**
 * New data on disk, as a live signal. A running widget session collects it, so a sync
 * redraws open widgets at once; DATA_VERSION covers widgets whose session has closed.
 */
object WidgetData {
    val version = MutableStateFlow(0)

    /** The data version a widget should draw: changes whenever either signal does. */
    @Composable
    fun current(stored: Int?): Int {
        val live by version.collectAsState()
        return live * 100_000 + (stored ?: 0)
    }
}

private const val LAUNCHER = "com.google.androidbrowserhelper.trusted.LauncherActivity"

/** Opens a Tether page (a path such as /tasks?task=12) in the app. */
fun openInApp(context: Context, path: String): Action = actionStartActivity(
    Intent(Intent.ACTION_VIEW, Uri.parse(Api.BASE + path))
        .setClassName(context.packageName, LAUNCHER)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))

fun parse(hex: String?): Color? = hex?.let { runCatching { Color(android.graphics.Color.parseColor(it)) }.getOrNull() }

fun color(hex: String?, fallback: Color = Color(0xFF8A8F98)): ColorProvider = ColorProvider(parse(hex) ?: fallback)

/** An account or tag color, see-through, as a row's tint. */
fun tint(hex: String?, alpha: Float = 0.22f): ColorProvider =
    ColorProvider((parse(hex) ?: Color(0xFF8A8F98)).copy(alpha = alpha))

val Muted = 12.sp
val Body = 14.sp

/** Widget background: the app's own page background (the bg variable in web/src/index.css) in each mode. */
val Surface = DayNightColorProvider(day = Color(0xFFFFFFFF), night = Color(0xFF12151A))

/** The rounded widget surface; text and accents still follow the wallpaper's colors. */
@Composable
fun WidgetFrame(content: @Composable ColumnScope.() -> Unit) {
    GlanceTheme {
        Column(
            GlanceModifier.fillMaxSize().appWidgetBackground().cornerRadius(20.dp)
                .background(Surface).padding(horizontal = 12.dp, vertical = 10.dp),
        ) { content() }
    }
}

/** Title row: a title that opens a page, then icon buttons. */
@Composable
fun Header(title: String, open: Action, vararg buttons: Pair<Int, Action>) {
    Row(GlanceModifier.fillMaxWidth().padding(bottom = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, maxLines = 1, modifier = GlanceModifier.defaultWeight().clickable(open),
             style = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.Medium, color = GlanceTheme.colors.onSurface))
        for ((icon, action) in buttons) IconButton(icon, action)
    }
}

@Composable
fun IconButton(icon: Int, action: Action) {
    Image(ImageProvider(icon), contentDescription = null,
          colorFilter = ColorFilter.tint(GlanceTheme.colors.onSurfaceVariant),
          modifier = GlanceModifier.size(36.dp).padding(6.dp).cornerRadius(18.dp).clickable(action))
}

/** What a widget shows instead of data: signed out, or nothing fetched yet. */
@Composable
fun Placeholder(context: Context, empty: String) {
    val signedIn = Tokens.signedIn(context)
    Column(GlanceModifier.fillMaxSize(), verticalAlignment = Alignment.CenterVertically,
           horizontalAlignment = Alignment.CenterHorizontally) {
        Text(if (signedIn) empty else "Sign in to show Tether here",
             style = TextStyle(fontSize = Body, textAlign = TextAlign.Center, color = GlanceTheme.colors.onSurfaceVariant))
        if (!signedIn) {
            Spacer(GlanceModifier.height(8.dp))
            Button("Sign in", onClick = actionStartActivity<SignInActivity>())
        }
    }
}

/** A quiet line when the last sync failed (the widget still shows the last data it had). */
@Composable
fun SyncNote() {
    val context = LocalContext.current
    val note = when (Store.status(context)) {
        "signed_out" -> "Signed out · open a widget's Sign in"
        else -> return
    }
    Text(note, maxLines = 1, modifier = GlanceModifier.padding(top = 4.dp),
         style = TextStyle(fontSize = 11.sp, color = GlanceTheme.colors.onSurfaceVariant))
}

/** Refresh, or a crossed-out cloud when the last sync couldn't reach Tether (tap still retries). */
@Composable
fun refreshIcon(): Int =
    if (Store.status(LocalContext.current) == "offline") R.drawable.ic_cloud_off else R.drawable.ic_refresh

class RefreshAction : ActionCallback {
    override suspend fun onAction(context: Context, glanceId: GlanceId, parameters: ActionParameters) {
        Sync.run(context)
    }
}

/** Every widget receiver: keep the background sync scheduled, and sync when a widget appears. */
abstract class TetherReceiver : GlanceAppWidgetReceiver() {
    override fun onUpdate(context: Context, appWidgetManager: android.appwidget.AppWidgetManager, appWidgetIds: IntArray) {
        super.onUpdate(context, appWidgetManager, appWidgetIds)
        Sync.schedule(context)
        Sync.now(context)
    }
}
