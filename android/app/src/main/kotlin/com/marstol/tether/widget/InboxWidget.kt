package com.marstol.tether.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import androidx.datastore.preferences.core.Preferences
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.LocalContext
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.lazy.LazyColumn
import androidx.glance.appwidget.lazy.items
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.currentState
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.compose.ui.unit.sp
import com.marstol.tether.data.InboxItem
import com.marstol.tether.data.Store
import com.marstol.tether.data.Sync
import com.marstol.tether.data.parseInbox
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle

/**
 * Open mail from every account (or one), newest first. Read-only on purpose: no archive
 * button, so a stray touch on the home screen can't lose an email.
 */
class InboxWidget : GlanceAppWidget() {
    override suspend fun provideGlance(context: Context, id: GlanceId) {
        provideContent {
            val state = currentState<Preferences>()
            Content(state[ACCOUNT], WidgetData.current(state[DATA_VERSION]))
        }
    }

    @Composable
    @Suppress("UNUSED_PARAMETER")   // version: a new value means new data on disk
    private fun Content(account: String?, version: Int) {
        val context = LocalContext.current
        val body = Store.read(context, Store.inboxName(account))
        val inbox = body?.takeUnless { it.contains("\"missing\"") }?.let { runCatching { parseInbox(it) }.getOrNull() }
        val page = "/inbox" + (account?.let { "?account=" + android.net.Uri.encode(it) } ?: "")
        WidgetFrame {
            val shown = inbox?.items
            val count = inbox?.count
            Header(listOfNotNull(account ?: "Inbox", count?.toString()).joinToString("  "), openInApp(context, page),
                   refreshIcon() to actionRunCallback<RefreshAction>())
            when {
                body != null && inbox == null -> Placeholder(context, "That account is gone. Long-press the widget to pick another.")
                shown == null -> Placeholder(context, "Loading mail…")
                shown.isEmpty() -> Placeholder(context, "Inbox zero")
                else -> LazyColumn(GlanceModifier.defaultWeight()) {
                    items(shown, itemId = { it.id.toLong() }) { MailRow(it, showAccount = account == null) }
                }
            }
            SyncNote()
        }
    }

    @Composable
    private fun MailRow(item: InboxItem, showAccount: Boolean) {
        val context = LocalContext.current
        // The gap between rows comes from this wrapper: padding and background share one view in Glance.
        Box(GlanceModifier.fillMaxWidth().padding(vertical = 3.dp)) {
            Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                // The account's color, as in the web inbox.
                Box(GlanceModifier.width(3.dp).height(50.dp).cornerRadius(2.dp).background(color(item.color))) {}
                Spacer(GlanceModifier.width(8.dp))
                Column(GlanceModifier.defaultWeight().clickable(openInApp(context, item.url))) {
                    Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text((if (item.out) "↗ " else "") + item.sender, maxLines = 1,
                             modifier = GlanceModifier.defaultWeight(),
                             style = TextStyle(fontSize = Body, fontWeight = FontWeight.Medium, color = GlanceTheme.colors.onSurface))
                        if (item.high) Badge("High", GlanceTheme.colors.error)
                        if (item.task) Badge("task", GlanceTheme.colors.primary)
                        Text(whenLabel(item.at), maxLines = 1, modifier = GlanceModifier.padding(start = 6.dp),
                             style = TextStyle(fontSize = Muted, color = GlanceTheme.colors.onSurfaceVariant))
                    }
                    Text(item.title, maxLines = 1, style = TextStyle(fontSize = 13.sp, color = GlanceTheme.colors.onSurface))
                    val sub = listOfNotNull(if (showAccount) item.account else null, item.snippet).joinToString(" · ")
                    if (sub.isNotEmpty()) Text(sub, maxLines = 1,
                        style = TextStyle(fontSize = Muted, color = GlanceTheme.colors.onSurfaceVariant))
                }
            }
        }
    }

    @Composable
    private fun Badge(text: String, tone: androidx.glance.unit.ColorProvider) {
        Text(text, maxLines = 1, modifier = GlanceModifier.padding(start = 6.dp),
             style = TextStyle(fontSize = 11.sp, fontWeight = FontWeight.Medium, color = tone))
    }

    private fun whenLabel(iso: String): String {
        val at = Instant.parse(iso).atZone(ZoneId.systemDefault())
        val today = LocalDate.now()
        return when {
            at.toLocalDate() == today -> at.format(DateTimeFormatter.ofLocalizedTime(FormatStyle.SHORT))
            at.year == today.year -> at.format(DateTimeFormatter.ofPattern("MMM d"))
            else -> at.format(DateTimeFormatter.ofPattern("M/d/yy"))
        }
    }
}

class InboxWidgetReceiver : TetherReceiver() {
    override val glanceAppWidget = InboxWidget()
}
