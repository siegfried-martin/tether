package com.marstol.tether.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.datastore.preferences.core.Preferences
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.LocalContext
import androidx.glance.LocalSize
import androidx.glance.action.ActionParameters
import androidx.glance.action.actionParametersOf
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.lazy.LazyColumn
import androidx.glance.appwidget.lazy.items
import androidx.glance.appwidget.provideContent
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.background
import androidx.glance.currentState
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.size
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import com.marstol.tether.R
import com.marstol.tether.data.Event
import com.marstol.tether.data.Store
import com.marstol.tether.data.Sync
import com.marstol.tether.data.parseCalendar
import java.time.Duration
import java.time.LocalDate
import java.time.ZoneId
import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.min

/** One day as a timeline, with arrows to step through the days Tether has synced. */
class DayWidget : GlanceAppWidget() {
    override val sizeMode = SizeMode.Exact

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        provideContent {
            val state = currentState<Preferences>()
            Content(state[DAY_OFFSET] ?: 0, state[DATA_VERSION] ?: 0)
        }
    }

    /** A timed event placed on the grid: minutes from midnight, and its lane among overlapping events. */
    private data class Placed(val event: Event, val from: Int, val to: Int, var lane: Int = 0, var lanes: Int = 1)

    @Composable
    @Suppress("UNUSED_PARAMETER")   // version: a new value means new data on disk
    private fun Content(offset: Int, version: Int) {
        val context = LocalContext.current
        val size = LocalSize.current
        val cal = Store.read(context, Store.CALENDAR)?.let { runCatching { parseCalendar(it) }.getOrNull() }
        WidgetFrame {
            if (cal == null) {
                Header("Day", openInApp(context, "/calendar/day"), refreshIcon to actionRunCallback<RefreshAction>())
                Placeholder(context, "Loading calendar…")
                return@WidgetFrame
            }
            val date = cal.today.plusDays(offset.toLong())
            val day = cal.days.firstOrNull { it.date == date }
            val first = cal.days.first().date
            val last = cal.days.last().date
            val shift = { by: Int -> actionRunCallback<ShiftDay>(actionParametersOf(ShiftDay.BY to by)) }
            val buttons = buildList {
                if (offset != 0) add(R.drawable.ic_today to shift(0))
                if (date > first) add(R.drawable.ic_chevron_left to shift(-1))
                if (date < last) add(R.drawable.ic_chevron_right to shift(1))
                add(refreshIcon to actionRunCallback<RefreshAction>())
            }
            Header(dayName(date, cal.today), openInApp(context, "/calendar/day?date=$date"), *buttons.toTypedArray())

            val events = day?.events ?: emptyList()
            val allDay = events.filter { it.allDay || (it.continued && it.continues) }
            // Scrolls: all-day events first, then the timeline at a readable hour height.
            LazyColumn(GlanceModifier.defaultWeight()) {
                items(allDay, itemId = { -it.id.toLong() }) { Chip(it) }
                item(itemId = Long.MAX_VALUE) {
                    Timeline(date, cal.today, events - allDay.toSet(), width = size.width - 24.dp)
                }
            }
            SyncNote()
        }
    }

    @Composable
    private fun Chip(e: Event) {
        val context = LocalContext.current
        Box(GlanceModifier.fillMaxWidth().padding(bottom = 2.dp).clickable(openInApp(context, e.url))) {
            Text(e.title, maxLines = 1,
                 modifier = GlanceModifier.fillMaxWidth().cornerRadius(6.dp).background(color(e.color))
                     .padding(horizontal = 8.dp, vertical = 3.dp),
                 style = TextStyle(fontSize = Muted, fontWeight = FontWeight.Medium, color = ColorProvider(Color.White)))
        }
    }

    @Composable
    private fun Timeline(date: LocalDate, today: LocalDate, events: List<Event>, width: Dp) {
        val context = LocalContext.current
        val zone = ZoneId.systemDefault()
        val midnight = date.atStartOfDay(zone)
        val minutes = { iso: String -> Duration.between(midnight, localTime(iso)).toMinutes().toInt().coerceIn(0, 1440) }
        val placed = events.map { e ->
            val from = minutes(e.start)
            Placed(e, from, max(minutes(e.end ?: e.start), from + 15))
        }.sortedBy { it.from }
        assignLanes(placed)

        // Hours shown: 8am to 8pm, widened to fit the day's events. The list starts at the top,
        // so the first hour shown is the first one that matters.
        val nowMin = if (date == today) Duration.between(midnight, java.time.ZonedDateTime.now(zone)).toMinutes().toInt() else null
        val startH = min(8, placed.minOfOrNull { it.from / 60 } ?: 8)
        val endH = min(24, max(20, placed.maxOfOrNull { ceil(it.to / 60.0).toInt() } ?: 20))
        val hourH = HOUR_DP
        val height = ((endH - startH) * hourH).dp
        val y = { m: Int -> ((m - startH * 60) * hourH / 60f).coerceIn(0f, height.value) }
        val gutter = 34f
        val area = width.value - gutter

        Box(GlanceModifier.fillMaxWidth().height(height)) {
            // Hour lines, in groups of 8 (a Glance container holds at most 10 children).
            Column(GlanceModifier.fillMaxSize()) {
                for (group in (startH until endH).chunked(8)) {
                    Column(GlanceModifier.fillMaxWidth()) {
                        for (h in group) HourRow(h, hourH.dp)
                    }
                }
            }
            for (group in placed.chunked(8)) {
                Box(GlanceModifier.fillMaxSize()) {
                    for (p in group) {
                        val top = y(p.from)
                        val h = max(y(p.to) - top - 1, 16f)
                        val w = area / p.lanes
                        Box(GlanceModifier.fillMaxSize().padding(start = (gutter + w * p.lane).dp, top = top.dp)) {
                            Column(GlanceModifier.size((w - 2).dp, h.dp).cornerRadius(5.dp)
                                       .background(color(p.event.color)).padding(horizontal = 5.dp, vertical = 2.dp)
                                       .clickable(openInApp(context, p.event.url))) {
                                Text(p.event.title, maxLines = max(1, ((h - 4) / 14).toInt()),
                                     style = TextStyle(fontSize = 11.sp, fontWeight = FontWeight.Medium,
                                                       color = ColorProvider(Color.White)))
                                if (h >= 44 && p.lanes == 1) Text(timeLabel(p.event), maxLines = 1,
                                     style = TextStyle(fontSize = 10.sp, color = ColorProvider(Color(0xDDFFFFFF))))
                            }
                        }
                    }
                }
            }
            if (nowMin != null && nowMin in startH * 60..endH * 60) {
                Box(GlanceModifier.fillMaxSize().padding(start = (gutter - 4).dp, top = y(nowMin).dp)) {
                    Box(GlanceModifier.fillMaxWidth().height(2.dp).background(GlanceTheme.colors.error)) {}
                }
            }
        }
    }

    private companion object {
        const val HOUR_DP = 48f
    }

    @Composable
    private fun HourRow(hour: Int, height: Dp) {
        Row(GlanceModifier.fillMaxWidth().height(height), verticalAlignment = Alignment.Top) {
            Text(hourLabel(hour), modifier = GlanceModifier.width(34.dp),
                 style = TextStyle(fontSize = 10.sp, color = GlanceTheme.colors.onSurfaceVariant))
            Box(GlanceModifier.defaultWeight().height(1.dp).background(GlanceTheme.colors.outline)) {}
        }
    }

    private fun hourLabel(h: Int) = when {
        h == 0 || h == 24 -> "12a"
        h < 12 -> "${h}a"
        h == 12 -> "12p"
        else -> "${h - 12}p"
    }

    /** Side by side when events overlap: each cluster of overlapping events shares its lanes. */
    private fun assignLanes(placed: List<Placed>) {
        var cluster = mutableListOf<Placed>()
        var clusterEnd = -1
        val ends = mutableListOf<Int>()
        fun close() {
            cluster.forEach { it.lanes = ends.size.coerceAtLeast(1) }
            cluster = mutableListOf(); ends.clear()
        }
        for (p in placed) {
            if (p.from >= clusterEnd) close()
            val lane = ends.indexOfFirst { it <= p.from }
            if (lane >= 0) { p.lane = lane; ends[lane] = p.to } else { p.lane = ends.size; ends.add(p.to) }
            cluster.add(p)
            clusterEnd = max(clusterEnd, p.to)
        }
        close()
    }
}

class ShiftDay : ActionCallback {
    override suspend fun onAction(context: Context, glanceId: GlanceId, parameters: ActionParameters) {
        val by = parameters[BY] ?: return
        updateAppWidgetState(context, glanceId) { prefs ->
            prefs[DAY_OFFSET] = if (by == 0) 0 else ((prefs[DAY_OFFSET] ?: 0) + by).coerceIn(-1, Sync.CALENDAR_DAYS - 2)
        }
        DayWidget().update(context, glanceId)
    }

    companion object {
        val BY = ActionParameters.Key<Int>("by")
    }
}

class DayWidgetReceiver : TetherReceiver() {
    override val glanceAppWidget = DayWidget()
}
