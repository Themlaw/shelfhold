package eu.kanade.tachiyomi.extension.all.synthetic

import eu.kanade.tachiyomi.network.GET
import eu.kanade.tachiyomi.source.model.FilterList
import eu.kanade.tachiyomi.source.model.MangasPage
import eu.kanade.tachiyomi.source.model.Page
import eu.kanade.tachiyomi.source.model.SChapter
import eu.kanade.tachiyomi.source.model.SManga
import eu.kanade.tachiyomi.source.online.HttpSource
import okhttp3.Request
import okhttp3.Response
import org.json.JSONArray

/**
 * A source that reads the fixture server of the test bench (http://fixtures:8000) and nothing
 * else: invented series, generated pages. It exists so that the real download queue can be
 * played without any third-party content (ENF-LEG-013).
 */
class Synthetic : HttpSource() {
    override val name = "Synthetic"
    override val baseUrl = "http://fixtures:8000"
    override val lang = "all"
    override val supportsLatest = false

    override fun popularMangaRequest(page: Int): Request = GET("$baseUrl/series", headers)

    override fun popularMangaParse(response: Response): MangasPage {
        val rows = JSONArray(response.body.string())
        val mangas = (0 until rows.length()).map {
            val row = rows.getJSONObject(it)
            SManga.create().apply {
                url = "/series/${row.getString("slug")}"
                title = row.getString("title")
            }
        }
        return MangasPage(mangas, false)
    }

    override fun latestUpdatesRequest(page: Int): Request = throw UnsupportedOperationException()
    override fun latestUpdatesParse(response: Response): MangasPage = throw UnsupportedOperationException()

    override fun searchMangaRequest(page: Int, query: String, filters: FilterList): Request =
        popularMangaRequest(page)

    override fun searchMangaParse(response: Response): MangasPage = popularMangaParse(response)

    override fun mangaDetailsParse(response: Response): SManga = SManga.create()

    override fun chapterListRequest(manga: SManga): Request = GET("$baseUrl${manga.url}/chapters", headers)

    override fun chapterListParse(response: Response): List<SChapter> {
        val slug = response.request.url.pathSegments[1]
        val rows = JSONArray(response.body.string())
        return (0 until rows.length()).map {
            val row = rows.getJSONObject(it)
            SChapter.create().apply {
                url = "/pages/$slug/${row.getString("id")}"
                name = row.getString("name")
                chapter_number = row.getDouble("number").toFloat()
            }
        }.reversed()
    }

    override fun pageListRequest(chapter: SChapter): Request = GET("$baseUrl${chapter.url}", headers)

    override fun pageListParse(response: Response): List<Page> {
        val urls = JSONArray(response.body.string())
        return (0 until urls.length()).map { Page(it, imageUrl = urls.getString(it)) }
    }

    override fun imageUrlParse(response: Response): String = throw UnsupportedOperationException()
}
