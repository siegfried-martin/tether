package com.marstol.tether.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.datastore.preferences.core.Preferences
import androidx.glance.GlanceId
import androidx.glance.currentState
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
import com.marstol.tether.data.Event
import com.marstol.tether.data.Store
import com.marstol.tether.data.parseCalendar
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle

/** The coming week, day by day: today always, other days when they have something on. */
class AgendaWidget : GlanceAppWidget() {
    override suspend fun provideGlance(context: Context, id: GlanceId) {
        provideContent { Content(currentState<Preferences>()[DATA_VERSION] ?: 0) }
    }

    private sealed interface Line {
        val key: Long
        data class Head(val date: LocalDate, val today: LocalDate, val empty: Boolean) : Line {
            override val key get() = -date.toEpochDay()
        }
        data class Item(val event: Event, val date: LocalDate) : Line {
            override val key get() = date.toEpochDay() * 100_000 + event.id
        }
    }

    @Composable
    @Suppress("UNUSED_PARAMETER")   // version: a new value means new data on disk
    private fun Content(version: Int) {
        val context = LocalContext.current
        val cal = Store.read(context, Store.CALENDAR)?.let { runCatching { parseCalendar(it) }.getOrNull() }
        WidgetFrame {
            Header("Agenda", openInApp(context, "/calendar/schedule"), refreshIcon to actionRunCallback<RefreshAction>())
            if (cal == null) {
                Placeholder(context, "Loading calendar…")
                return@WidgetFrame
            }
            val lines = cal.days.filter { it.date >= cal.today && it.date < cal.today.plusDays(7) }
                .filter { it.date == cal.today || it.events.isNotEmpty() }
                .flatMap { day ->
                    listOf<Line>(Line.Head(day.date, cal.today, day.events.isEmpty())) +
                        day.events.map { Line.Item(it, day.date) }
                }
            LazyColumn(GlanceModifier.defaultWeight()) {
                items(lines, itemId = { it.key }) { line ->
                    when (line) {
                        is Line.Head -> DayHead(line)
                        is Line.Item -> EventRow(line.event)
                    }
                }
            }
            SyncNote()
        }
    }

    @Composable
    private fun DayHead(head: Line.Head) {
        Column(GlanceModifier.fillMaxWidth().padding(top = 6.dp, bottom = 2.dp)) {
            Text(dayName(head.date, head.today),
                 style = TextStyle(fontSize = 13.sp, fontWeight = FontWeight.Medium, color = GlanceTheme.colors.primary))
            if (head.empty) Text("Nothing scheduled",
                                 style = TextStyle(fontSize = Muted, color = GlanceTheme.colors.onSurfaceVariant))
        }
    }

    @Composable
    private fun EventRow(event: Event) {
        val context = LocalContext.current
        // The gap between rows comes from this wrapper: padding and background share one view in Glance.
        Box(GlanceModifier.fillMaxWidth().padding(vertical = 2.dp)) {
            Row(GlanceModifier.fillMaxWidth().cornerRadius(8.dp).background(tint(event.color))
                    .clickable(openInApp(context, event.url)),
                verticalAlignment = Alignment.CenterVertically) {
                Box(GlanceModifier.width(4.dp).height(42.dp).background(color(event.color))) {}
                Spacer(GlanceModifier.width(8.dp))
                Column(GlanceModifier.defaultWeight()) {
                    Text(event.title, maxLines = 1, style = TextStyle(fontSize = Body, color = GlanceTheme.colors.onSurface))
                    val detail = listOfNotNull(timeLabel(event), event.location?.takeIf { it.isNotBlank() }).joinToString(" · ")
                    Text(detail, maxLines = 1, style = TextStyle(fontSize = Muted, color = GlanceTheme.colors.onSurfaceVariant))
                }
            }
        }
    }
}

private val shortTime = DateTimeFormatter.ofLocalizedTime(FormatStyle.SHORT)

fun localTime(iso: String) = Instant.parse(iso).atZone(ZoneId.systemDefault())

fun timeLabel(e: Event): String = when {
    e.allDay -> if (e.continued || e.continues) "All day (multi-day)" else "All day"
    e.continued && e.continues -> "All day (continues)"
    e.continued -> "Until ${localTime(e.end ?: e.start).format(shortTime)}"
    e.continues -> "${localTime(e.start).format(shortTime)} onward"
    else -> localTime(e.start).format(shortTime) + (e.end?.let { " – " + localTime(it).format(shortTime) } ?: "")
}

fun dayName(date: LocalDate, today: LocalDate): String {
    val label = date.format(DateTimeFormatter.ofPattern("EEE, MMM d"))
    return when (date) {
        today -> "Today · $label"
        today.plusDays(1) -> "Tomorrow · $label"
        today.minusDays(1) -> "Yesterday · $label"
        else -> label
    }
}

class AgendaWidgetReceiver : TetherReceiver() {
    override val glanceAppWidget = AgendaWidget()
}
