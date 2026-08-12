import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import uuid
from datetime import datetime, timezone, timedelta

# Import shared functions from bot.py
from bot import (
    d1_query, 
    GUILD_ID,
    FOUNDER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
    LINKED_ROLE_ID,
    LOG_CHANNELS,
    is_hoster,
    is_elite_hoster,
    is_staff,
    is_mod_or_higher,
    is_head_staff_or_founder,
    is_linked,
    is_hoster_or_higher
)

# ── Channel IDs ──────────────────────────────────────────────────────────────
SHOP_PANEL_CHANNEL_ID = 1535562484788891658
SHOP_ORDER_CATEGORY_ID = 1535922322974838824

# ── Staff Roles ──────────────────────────────────────────────────────────────
STAFF_ROLES = {
    FOUNDER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID
}


# ── Logging Helper ───────────────────────────────────────────────────────────
async def send_log(bot: commands.Bot, embed: discord.Embed):
    """Send a log message to the shop log channel"""
    channel = bot.get_channel(LOG_CHANNELS.get("shop", 0))
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending log: {e}")


# ── Shop Database Functions ──────────────────────────────────────────────────
async def get_shop_items():
    """Get all shop items from database"""
    result = await d1_query(
        "SELECT id, name, cost FROM shop_items ORDER BY cost ASC, id ASC"
    )
    return result["results"]


async def get_shop_item(item_id: int):
    """Get a specific shop item"""
    result = await d1_query(
        "SELECT id, name, cost FROM shop_items WHERE id = ?",
        [item_id]
    )
    if result["results"]:
        return result["results"][0]
    return None


async def add_shop_item(name: str, cost: int):
    """Add a new shop item"""
    now = datetime.now(timezone.utc).isoformat()
    await d1_query(
        "INSERT INTO shop_items (name, cost, created_at) VALUES (?, ?, ?)",
        [name, cost, now]
    )


async def remove_shop_item(item_id: int):
    """Remove a shop item"""
    await d1_query(
        "DELETE FROM shop_items WHERE id = ?",
        [item_id]
    )


async def update_shop_item(item_id: int, field: str, value):
    """Update a shop item field"""
    await d1_query(
        f"UPDATE shop_items SET {field} = ? WHERE id = ?",
        [value, item_id]
    )


async def get_user_lifetime_points(user_id: int) -> int:
    """Get a user's current balance (lifetime points that can be spent)"""
    result = await d1_query(
        "SELECT hoster_points FROM users WHERE discord_id = ?",
        [str(user_id)]
    )
    if result["results"]:
        return result["results"][0]["hoster_points"] or 0
    return 0


async def get_user_total_earned(user_id: int) -> int:
    """Get a user's total points earned (lifetime, never decreases)"""
    result = await d1_query(
        "SELECT total_points_earned FROM users WHERE discord_id = ?",
        [str(user_id)]
    )
    if result["results"]:
        return result["results"][0]["total_points_earned"] or 0
    return 0


async def spend_user_points(user_id: int, amount: int) -> int:
    """Spend points from a user (only subtracts from balance, not total earned)"""
    current = await get_user_lifetime_points(user_id)
    if current < amount:
        return -1  # Not enough points

    new_total = current - amount
    await d1_query(
        "UPDATE users SET hoster_points = ? WHERE discord_id = ?",
        [new_total, str(user_id)]
    )
    return new_total


# ── Views ────────────────────────────────────────────────────────────────────
class ShopPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🛒 Buy Item",
        style=discord.ButtonStyle.success,
        emoji="🛒",
        custom_id="shop_buy"
    )
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not is_hoster(interaction.user) and not is_staff(interaction.user):
                return await interaction.response.send_message(
                    "❌ You need the Hoster role to use the shop.",
                    ephemeral=True
                )
            if not is_linked(interaction.user):
                return await interaction.response.send_message(
                    "❌ You need to be verified first before using the shop.",
                    ephemeral=True
                )
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return

        # Get shop items
        items = await get_shop_items()
        if not items:
            return await interaction.followup.send(
                "❌ No items available in the shop.",
                ephemeral=True
            )

        # Create the selection view
        view = ShopSelectView(items=items)
        embed = discord.Embed(
            title="🛒 Select an Item",
            description="Choose an item to purchase from the dropdown below.",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )

        # Show user's current points (balance) and total earned
        points = await get_user_lifetime_points(interaction.user.id)
        total_earned = await get_user_total_earned(interaction.user.id)
        embed.set_footer(text=f"Balance: {points} pts | Total Earned: {total_earned} pts")

        await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True
        )


class ShopSelectView(discord.ui.View):
    def __init__(self, items: list):
        super().__init__(timeout=120)
        self.items = items

        # Create dropdown options
        options = []
        for item in items:
            options.append(
                discord.SelectOption(
                    label=f"{item['name']}",
                    description=f"Cost: {item['cost']} points",
                    value=str(item['id'])
                )
            )

        # Add the select menu
        self.select_item.options = options

    @discord.ui.select(
        placeholder="Select an item to purchase...",
        min_values=1,
        max_values=1
    )
    async def select_item(self, interaction: discord.Interaction, select: discord.ui.Select):
        item_id = int(select.values[0])
        item = await get_shop_item(item_id)
        if not item:
            return await interaction.response.send_message(
                "❌ Item not found.",
                ephemeral=True
            )

        # Show quantity modal
        await interaction.response.send_modal(ShopQuantityModal(
            item_id=item_id,
            item_name=item['name'],
            cost=item['cost']
        ))


class ShopQuantityModal(discord.ui.Modal, title="Enter Quantity"):
    def __init__(self, item_id: int, item_name: str, cost: int):
        super().__init__()
        self.item_id = item_id
        self.item_name = item_name
        self.cost = cost

    quantity = discord.ui.TextInput(
        label="Quantity",
        placeholder="Enter the quantity you want to purchase...",
        required=True,
        max_length=5,
        default="1"
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            quantity = int(self.quantity.value)
            if quantity < 1:
                return await interaction.followup.send(
                    "❌ Quantity must be at least 1.",
                    ephemeral=True
                )
            if quantity > 100:
                return await interaction.followup.send(
                    "❌ Maximum quantity is 100.",
                    ephemeral=True
                )

            total_cost = self.cost * quantity

            # Get user's current points (balance)
            user_points = await get_user_lifetime_points(interaction.user.id)
            total_earned = await get_user_total_earned(interaction.user.id)

            if user_points < total_cost:
                return await interaction.followup.send(
                    f"❌ You don't have enough points! You have **{user_points}** points, but need **{total_cost}**.\n\n"
                    f"💡 You need to earn more points by hosting raids!\n"
                    f"📊 Total Earned (Lifetime): **{total_earned}** points",
                    ephemeral=True
                )

            # Show confirmation
            embed = discord.Embed(
                title="🛒 Confirm Purchase",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Item", value=f"**{self.item_name}**", inline=True)
            embed.add_field(name="Quantity", value=str(quantity), inline=True)
            embed.add_field(name="Total Cost", value=f"**{total_cost}** points", inline=True)
            embed.add_field(name="Current Balance", value=f"**{user_points}** points", inline=True)
            embed.add_field(name="Total Earned (Lifetime)", value=f"**{total_earned}** points", inline=True)
            embed.add_field(name="Balance After", value=f"**{user_points - total_cost}** points", inline=True)
            embed.set_footer(text="Your total earned never decreases when you spend points!")

            view = ShopConfirmView(
                item_id=self.item_id,
                item_name=self.item_name,
                cost=self.cost,
                quantity=quantity,
                total_cost=total_cost,
                user_id=interaction.user.id
            )

            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except ValueError:
            await interaction.followup.send(
                "❌ Please enter a valid number.",
                ephemeral=True
            )


class ShopConfirmView(discord.ui.View):
    def __init__(self, item_id: int, item_name: str, cost: int,
                 quantity: int, total_cost: int, user_id: int):
        super().__init__(timeout=120)
        self.item_id = item_id
        self.item_name = item_name
        self.cost = cost
        self.quantity = quantity
        self.total_cost = total_cost
        self.user_id = user_id

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ This is not your purchase.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # Check points again (in case they spent them elsewhere)
        user_points = await get_user_lifetime_points(self.user_id)
        if user_points < self.total_cost:
            return await interaction.followup.send(
                f"❌ You no longer have enough points! You have **{user_points}** points.",
                ephemeral=True
            )

        # Deduct points (only from balance, total_earned stays the same)
        new_balance = await spend_user_points(self.user_id, self.total_cost)
        if new_balance < 0:
            return await interaction.followup.send(
                "❌ Failed to deduct points. Please try again.",
                ephemeral=True
            )

        # Get total earned for display
        total_earned = await get_user_total_earned(self.user_id)

        # Get the shop category
        category = interaction.guild.get_channel(SHOP_ORDER_CATEGORY_ID)
        if not category:
            return await interaction.followup.send(
                "❌ Order category not found. Please contact staff.",
                ephemeral=True
            )

        # Create a private channel for the order
        channel_name = f"order-{interaction.user.name[:15]}-{str(uuid.uuid4())[:4]}"

        # Set up permissions
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_permissions=True
            )
        }

        # Add staff role access
        for role_id in STAFF_ROLES:
            role = interaction.guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True
                )

        try:
            channel = await interaction.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Shop order for {interaction.user} - {self.item_name}"
            )
        except Exception as e:
            return await interaction.followup.send(
                f"❌ Failed to create order channel: {e}",
                ephemeral=True
            )

        # Send the order embed
        embed = discord.Embed(
            title="🛒 New Shop Order",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Customer", value=interaction.user.mention, inline=True)
        embed.add_field(name="Item", value=f"**{self.item_name}**", inline=True)
        embed.add_field(name="Quantity", value=str(self.quantity), inline=True)
        embed.add_field(name="Total Cost", value=f"**{self.total_cost}** points", inline=True)
        embed.add_field(name="New Balance", value=f"**{new_balance}** points", inline=True)
        embed.add_field(name="Total Earned (Lifetime)", value=f"**{total_earned}** points", inline=True)
        embed.set_footer(text=f"Order ID: {str(uuid.uuid4())[:8]}")

        # Ping Founder role
        founder_role = interaction.guild.get_role(FOUNDER_ROLE_ID)
        founder_ping = founder_role.mention if founder_role else "@here"

        view = ShopOrderControlView(
            user_id=self.user_id,
            channel_id=channel.id,
            item_name=self.item_name,
            quantity=self.quantity,
            total_cost=self.total_cost,
            new_balance=new_balance,
            total_earned=total_earned
        )

        await channel.send(
            content=f"{founder_ping} - New order from {interaction.user.mention}!",
            embed=embed,
            view=view
        )

        await interaction.followup.send(
            f"✅ Your order has been placed! Please wait in <#{channel.id}> for a staff member to assist you.\n"
            f"**Item:** {self.item_name}\n"
            f"**Quantity:** {self.quantity}\n"
            f"**Total Cost:** {self.total_cost} points\n"
            f"**New Balance:** {new_balance} points\n"
            f"**Total Earned:** {total_earned} points (never decreases!)",
            ephemeral=True
        )

        # Log the order
        log_embed = discord.Embed(
            title="🛒 New Shop Order",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Customer", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Item", value=self.item_name, inline=True)
        log_embed.add_field(name="Quantity", value=str(self.quantity), inline=True)
        log_embed.add_field(name="Total Cost", value=str(self.total_cost), inline=True)
        log_embed.add_field(name="Channel", value=f"<#{channel.id}>", inline=True)
        await send_log(interaction.client, log_embed)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ This is not your purchase.",
                ephemeral=True
            )

        await interaction.response.send_message(
            "❌ Purchase cancelled.",
            ephemeral=True
        )
        self.stop()


class ShopOrderControlView(discord.ui.View):
    def __init__(self, user_id: int, channel_id: int, item_name: str,
                 quantity: int, total_cost: int, new_balance: int, total_earned: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.channel_id = channel_id
        self.item_name = item_name
        self.quantity = quantity
        self.total_cost = total_cost
        self.new_balance = new_balance
        self.total_earned = total_earned

    @discord.ui.button(
        label="✅ Complete Order",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="shop_complete"
    )
    async def complete_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_head_staff_or_founder(interaction.user):
            try:
                await interaction.response.send_message("❌ Only Head Staff and Founder can complete orders.", ephemeral=True)
            except discord.HTTPException:
                pass
            return

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return

        # Get the channel
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel:
            return await interaction.followup.send(
                "❌ Order channel not found.",
                ephemeral=True
            )

        # Update the embed
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(
            name="✅ Completed",
            value=f"Order completed by {interaction.user.mention}",
            inline=False
        )

        # Disable buttons
        for child in self.children:
            child.disabled = True

        await interaction.message.edit(embed=embed, view=self)

        # DM the user
        try:
            user = interaction.guild.get_member(self.user_id)
            if user:
                dm_embed = discord.Embed(
                    title="✅ Order Completed!",
                    description=f"Your order for **{self.item_name}** x{self.quantity} has been completed!",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm_embed.add_field(name="Total Cost", value=f"{self.total_cost} points", inline=True)
                dm_embed.add_field(name="New Balance", value=f"{self.new_balance} points", inline=True)
                dm_embed.add_field(name="Total Earned", value=f"{self.total_earned} points", inline=True)
                await user.send(embed=dm_embed)
        except Exception:
            pass

        # Log the completion
        log_embed = discord.Embed(
            title="✅ Order Completed",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Customer", value=f"<@{self.user_id}>", inline=True)
        log_embed.add_field(name="Item", value=self.item_name, inline=True)
        log_embed.add_field(name="Quantity", value=str(self.quantity), inline=True)
        log_embed.add_field(name="Completed By", value=interaction.user.mention, inline=True)
        await send_log(interaction.client, log_embed)

        await interaction.followup.send("✅ Order completed! Channel will be deleted in 5 seconds.", ephemeral=True)
        
        # Delete the channel after 5 seconds
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Order {self.item_name} completed by {interaction.user}")
        except Exception as e:
            print(f"Error deleting channel: {e}")

    @discord.ui.button(
        label="❌ Cancel Order",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="shop_cancel"
    )
    async def cancel_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_head_staff_or_founder(interaction.user):
            try:
                await interaction.response.send_message("❌ Only Head Staff and Founder can cancel orders.", ephemeral=True)
            except discord.HTTPException:
                pass
            return

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return

        # Get the channel
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel:
            return await interaction.followup.send(
                "❌ Order channel not found.",
                ephemeral=True
            )

        # Update the embed
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(
            name="❌ Cancelled",
            value=f"Order cancelled by {interaction.user.mention}",
            inline=False
        )

        # Disable buttons
        for child in self.children:
            child.disabled = True

        await interaction.message.edit(embed=embed, view=self)

        # Refund points (add back to balance)
        await d1_query(
            "UPDATE users SET hoster_points = hoster_points + ? WHERE discord_id = ?",
            [self.total_cost, str(self.user_id)]
        )

        # Get updated balance and total earned
        new_balance = await get_user_lifetime_points(self.user_id)
        total_earned = await get_user_total_earned(self.user_id)

        # DM the user
        try:
            user = interaction.guild.get_member(self.user_id)
            if user:
                dm_embed = discord.Embed(
                    title="❌ Order Cancelled",
                    description=f"Your order for **{self.item_name}** x{self.quantity} has been cancelled.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm_embed.add_field(name="Reason", value="Order cancelled by staff", inline=True)
                dm_embed.add_field(name="Points Refunded", value=f"{self.total_cost} points", inline=True)
                dm_embed.add_field(name="New Balance", value=f"{new_balance} points", inline=True)
                dm_embed.add_field(name="Total Earned", value=f"{total_earned} points", inline=True)
                await user.send(embed=dm_embed)
        except Exception:
            pass

        # Log the cancellation
        log_embed = discord.Embed(
            title="❌ Order Cancelled",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Customer", value=f"<@{self.user_id}>", inline=True)
        log_embed.add_field(name="Item", value=self.item_name, inline=True)
        log_embed.add_field(name="Quantity", value=str(self.quantity), inline=True)
        log_embed.add_field(name="Cancelled By", value=interaction.user.mention, inline=True)
        await send_log(interaction.client, log_embed)

        await interaction.followup.send(
            "❌ Order cancelled. Points have been refunded. Channel will be deleted in 5 seconds.",
            ephemeral=True
        )
        
        # Delete the channel after 5 seconds
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Order {self.item_name} cancelled by {interaction.user}")
        except Exception as e:
            print(f"Error deleting channel: {e}")


# ── Shop Update Views ──────────────────────────────────────────────────────
class ShopUpdateView(discord.ui.View):
    def __init__(self, item_id: int, current_item: dict):
        super().__init__(timeout=120)
        self.item_id = item_id
        self.current_item = current_item

    @discord.ui.select(
        placeholder="Select what to update...",
        options=[
            discord.SelectOption(label="Name", description="Update the item name", value="name", emoji="📝"),
            discord.SelectOption(label="Cost", description="Update the item cost", value="cost", emoji="💰")
        ]
    )
    async def select_field(self, interaction: discord.Interaction, select: discord.ui.Select):
        field = select.values[0]
        await interaction.response.send_modal(ShopUpdateModal(
            item_id=self.item_id,
            field=field,
            current_item=self.current_item
        ))


class ShopUpdateModal(discord.ui.Modal, title="Update Shop Item"):
    def __init__(self, item_id: int, field: str, current_item: dict):
        super().__init__()
        self.item_id = item_id
        self.field = field
        self.current_item = current_item

        if field == "name":
            self.value_input = discord.ui.TextInput(
                label="New Name",
                placeholder=f"Current: {current_item['name']}",
                required=True,
                max_length=100
            )
        else:  # cost
            self.value_input = discord.ui.TextInput(
                label="New Cost",
                placeholder=f"Current: {current_item['cost']}",
                required=True,
                max_length=10
            )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            if self.field == "name":
                new_value = self.value_input.value
                await update_shop_item(self.item_id, "name", new_value)
                message = f"✅ Item name updated to **{new_value}**"
            else:  # cost
                new_value = int(self.value_input.value)
                if new_value <= 0:
                    return await interaction.followup.send(
                        "❌ Cost must be greater than 0.",
                        ephemeral=True
                    )
                await update_shop_item(self.item_id, "cost", new_value)
                message = f"✅ Item cost updated to **{new_value}** points"

            embed = discord.Embed(
                title="✅ Shop Item Updated",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Item", value=self.current_item['name'], inline=True)
            embed.add_field(name="Updated Field", value=self.field.capitalize(), inline=True)
            embed.add_field(name="New Value", value=str(new_value), inline=True)
            embed.add_field(name="Updated By", value=interaction.user.mention, inline=True)

            await interaction.followup.send(embed=embed, ephemeral=True)
            await send_log(interaction.client, embed)

        except ValueError:
            await interaction.followup.send(
                "❌ Invalid value. Please enter a valid number for cost.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


# ── Shop Cog ──────────────────────────────────────────────────────────────────
class Shop(commands.Cog):
    shop = app_commands.Group(name="shop", description="Shop commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Register persistent views on startup
        self.bot.loop.create_task(self.register_persistent_views())
        # Start auto-update task
        self.update_shop_panel.start()

    def cog_unload(self):
        self.update_shop_panel.cancel()

    async def register_persistent_views(self):
        """Register all persistent views on bot startup"""
        try:
            await self.bot.wait_until_ready()

            # Register Shop Panel View
            shop_view = ShopPanelView()
            self.bot.add_view(shop_view)
            print("✅ Registered persistent Shop Panel view")

            print("✅ Shop system ready")
        except Exception as e:
            print(f"Error registering shop views: {e}")

    @tasks.loop(minutes=5)
    async def update_shop_panel(self):
        """Auto-update the shop panel with latest items"""
        try:
            await self.bot.wait_until_ready()
            
            channel = self.bot.get_channel(SHOP_PANEL_CHANNEL_ID)
            if not channel:
                return
            
            # Get the latest shop panel message
            async for message in channel.history(limit=50):
                if message.author == self.bot.user and message.embeds:
                    embed = message.embeds[0]
                    if embed.title == "🛒 Shop":
                        # Get updated items
                        items = await get_shop_items()
                        items_text = ""
                        if items:
                            for item in items:
                                items_text += f"• **{item['name']}** - {item['cost']} points\n"
                        else:
                            items_text = "*No items available yet.*"
                        
                        # Update the embed - find the field index
                        found = False
                        for i, field in enumerate(embed.fields):
                            if field.name == "📋 Available Items":
                                embed.set_field_at(
                                    i,
                                    name="📋 Available Items",
                                    value=items_text,
                                    inline=False
                                )
                                found = True
                                break
                        
                        if not found:
                            # Field not found, add it
                            embed.add_field(
                                name="📋 Available Items",
                                value=items_text,
                                inline=False
                            )
                        
                        embed.timestamp = datetime.now(timezone.utc)
                        
                        await message.edit(embed=embed)
                        print("✅ Shop panel auto-updated")
                        break
                        
        except Exception as e:
            print(f"Error updating shop panel: {e}")

    # ── /shoplist ─────────────────────────────────────────────────────────────
    @shop.command(name="list", description="List all items in the shop (Hoster+ only)")
    @app_commands.checks.cooldown(1, 10)
    async def shop_list(self, interaction: discord.Interaction):
        """List all shop items"""
        # Hoster+ only
        if not is_hoster(interaction.user) and not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You need the Hoster role to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            items = await get_shop_items()

            if not items:
                return await interaction.followup.send(
                    "📭 No items available in the shop.",
                    ephemeral=True
                )

            embed = discord.Embed(
                title="📋 Shop Items",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )

            item_lines = []
            for item in items:
                item_lines.append(f"`#{item['id']}` **{item['name']}** — {item['cost']} points")

            embed.add_field(
                name=f"Available Items ({len(items)})",
                value="\n".join(item_lines),
                inline=False
            )

            # Show user's points
            points = await get_user_lifetime_points(interaction.user.id)
            total_earned = await get_user_total_earned(interaction.user.id)
            embed.set_footer(text=f"Balance: {points} pts | Total Earned: {total_earned} pts")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /ashop ──────────────────────────────────────────────────────────────
    @shop.command(name="add", description="Add an item to the shop (Head Staff+)")
    @app_commands.describe(name="Item name", cost="Item cost in points")
    @app_commands.checks.cooldown(1, 10)
    async def shop_add(self, interaction: discord.Interaction, name: str, cost: int):
        # Head Staff+ only
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ Only Head Staff and Founder can use this command.",
                ephemeral=True
            )

        if cost <= 0:
            return await interaction.response.send_message(
                "❌ Cost must be greater than 0.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            await add_shop_item(name, cost)

            embed = discord.Embed(
                title="✅ Item Added to Shop",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Item", value=name, inline=True)
            embed.add_field(name="Cost", value=f"{cost} points", inline=True)
            embed.add_field(name="Added By", value=interaction.user.mention, inline=True)

            await interaction.followup.send(embed=embed, ephemeral=True)
            await send_log(self.bot, embed)
            
            # Force an immediate panel update
            await self.update_shop_panel()

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /rshop ──────────────────────────────────────────────────────────────
    @shop.command(name="remove", description="Remove an item from the shop (Head Staff+)")
    @app_commands.describe(item_id="The ID of the item to remove")
    @app_commands.checks.cooldown(1, 10)
    async def shop_remove(self, interaction: discord.Interaction, item_id: int):
        # Head Staff+ only
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ Only Head Staff and Founder can use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            item = await get_shop_item(item_id)
            if not item:
                return await interaction.followup.send(
                    f"❌ Item with ID `{item_id}` not found.",
                    ephemeral=True
                )

            await remove_shop_item(item_id)

            embed = discord.Embed(
                title="🗑️ Item Removed from Shop",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Item", value=item['name'], inline=True)
            embed.add_field(name="Cost", value=f"{item['cost']} points", inline=True)
            embed.add_field(name="Removed By", value=interaction.user.mention, inline=True)

            await interaction.followup.send(embed=embed, ephemeral=True)
            await send_log(self.bot, embed)
            
            # Force an immediate panel update
            await self.update_shop_panel()

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /ushop ──────────────────────────────────────────────────────────────
    @shop.command(name="edit", description="Update a shop item's name or cost (Head Staff+)")
    @app_commands.describe(item_id="The ID of the item to update")
    @app_commands.checks.cooldown(1, 10)
    async def shop_edit(self, interaction: discord.Interaction, item_id: int):
        # Head Staff+ only
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ Only Head Staff and Founder can use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            item = await get_shop_item(item_id)
            if not item:
                return await interaction.followup.send(
                    f"❌ Item with ID `{item_id}` not found.",
                    ephemeral=True
                )

            view = ShopUpdateView(item_id=item_id, current_item=item)
            embed = discord.Embed(
                title=f"📝 Update Item: {item['name']}",
                description=f"**Current Cost:** {item['cost']} points\n\nSelect what you want to update:",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )

            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(Shop(bot))
    print("✅ Shop cog setup complete")