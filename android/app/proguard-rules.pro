# Glance creates widget action callbacks by class name.
-keep class * implements androidx.glance.appwidget.action.ActionCallback { <init>(); }
# WorkManager creates workers by class name.
-keep class * extends androidx.work.ListenableWorker { <init>(...); }

# Glance tells widgets apart by class name: R8 must not merge or rename them, or one
# widget's redraw lands on another (the Agenda widget turned into the Tasks widget).
-keep class * extends androidx.glance.appwidget.GlanceAppWidget
