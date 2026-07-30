package com.crayon.rhodey_app

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.RemoteViews
import es.antonborri.home_widget.HomeWidgetPlugin
import org.json.JSONArray
import org.json.JSONObject

class PulseStripWidgetProvider : AppWidgetProvider() {
    override fun onUpdate(context: Context, appWidgetManager: AppWidgetManager, appWidgetIds: IntArray) {
        for (appWidgetId in appWidgetIds) {
            val views = RemoteViews(context.packageName, R.layout.pulse_strip_widget)

            val prefs = HomeWidgetPlugin.getData(context)
            
            // Headline
            val headline = prefs.getString("headline", "Rhodey is ready.")
            views.setTextViewText(R.id.tv_headline, headline)
            
            // Headline tap -> surface
            val headlineIntent = Intent(context, MainActivity::class.java).apply {
                data = Uri.parse("rhodey://surface")
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            }
            val headlinePendingIntent = PendingIntent.getActivity(context, 1, headlineIntent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            views.setOnClickPendingIntent(R.id.tv_headline, headlinePendingIntent)

            // Insights Loop
            views.removeAllViews(R.id.ll_insights_container)
            
            val insightsJsonStr = prefs.getString("insights_json", "[]")
            try {
                val insightsArray = JSONArray(insightsJsonStr)
                for (i in 0 until insightsArray.length()) {
                    val itemObj = insightsArray.getJSONObject(i)
                    val text = itemObj.optString("text", "")
                    val link = itemObj.optString("link", "rhodey://surface")
                    
                    val itemView = RemoteViews(context.packageName, R.layout.pulse_insight_item)
                    itemView.setTextViewText(R.id.tv_insight_text, text)
                    
                    val itemIntent = Intent(context, MainActivity::class.java).apply {
                        data = Uri.parse(link)
                        flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                    }
                    val itemPendingIntent = PendingIntent.getActivity(context, 100 + i, itemIntent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
                    itemView.setOnClickPendingIntent(R.id.tv_insight_text, itemPendingIntent)
                    
                    views.addView(R.id.ll_insights_container, itemView)
                }
            } catch (e: Exception) {
                e.printStackTrace()
            }

            val containerIntent = Intent(context, MainActivity::class.java).apply {
                data = Uri.parse("rhodey://surface")
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            }
            val containerPendingIntent = PendingIntent.getActivity(context, 2, containerIntent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            views.setOnClickPendingIntent(R.id.widget_container, containerPendingIntent)

            appWidgetManager.updateAppWidget(appWidgetId, views)
        }
    }
}